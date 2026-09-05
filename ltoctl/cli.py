"""Typer/Rich command-line entry point for ltoctl.

The command tree is intentionally a thin presentation layer over the
hardware-independent services.  Canonical catalog changes, tape safety, and
recovery semantics live in the library modules; this module only handles
options, rendering, logging, and process exit codes.
"""

from __future__ import annotations

import functools
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .archive.obsolete import mark_obsolete
from .archive.reader import restore_archive
from .archive.reconcile import reconcile_tape
from .archive.verify import verify_archive, verify_tape
from .archive.writer import add_archive
from .catalog.index import rebuild_index, search_index
from .catalog.store import CatalogStore
from .catalog.validation import validate_catalog
from .command_logging import CommandLogger, result_fields
from .config import load_config
from .errors import (
    ArchiveError,
    CatalogError,
    LtoctlError,
    PlannerError,
    ReconcileError,
    SafetyError,
    ScanError,
    TapeError,
    VerificationError,
)
from .planner.executor import PlanSourceDriftError, apply_plan
from .planner.scanner import plan_sources
from .tape.linux_mt import LinuxTapeBackend
from .tape.service import init_tape
from .utils.atomic import atomic_write_text
from .utils.units import format_bytes

try:  # Typer/Rich are the only formal CLI dependencies.
    import click
    import typer
    from rich.console import Console
    from rich.table import Table

    _HAS_TYPER = True
    _CONSOLE = Console()
except ImportError:  # pragma: no cover - exercised in a stdlib-only checkout
    click = None  # type: ignore[assignment]
    typer = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment,misc]
    _HAS_TYPER = False
    _CONSOLE = None


def _is_no_args_is_help(exc: BaseException) -> bool:
    """True for Click/Typer empty-group help, including Typer's vendored Click."""

    return type(exc).__name__ == "NoArgsIsHelpError"


def _print_group_help(exc: BaseException) -> None:
    """Show the invoked group's help without an ``ltoctl: error:`` prefix.

    Typer's vendored ``NoArgsIsHelpError`` already prints Rich help while
    the exception is constructed and leaves an empty message. Stock Click
    stores the help text on the exception; write that to stderr.
    """

    message = ""
    format_message = getattr(exc, "format_message", None)
    if callable(format_message):
        message = format_message() or ""
    if not message:
        rendered = str(exc)
        if rendered and rendered != "NoArgsIsHelpError":
            message = rendered
    if message:
        print(message, file=sys.stderr)


def _exit_code_for_error(exc: BaseException) -> int:
    """Return the stable process code for a typed service failure."""

    # SafetyError is a TapeError subclass, so it must be tested first.
    if isinstance(exc, SafetyError):
        return 3
    if isinstance(exc, TapeError):
        return 4
    if isinstance(exc, (VerificationError, ReconcileError, ArchiveError, OSError)):
        return 4
    if isinstance(exc, (CatalogError, PlannerError, ScanError, ValueError)):
        return 2
    return 1


def _json_mode(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("json_mode", kwargs.get("json", False)))


def _value_for_log(value: Any) -> Any:
    """Convert an option value to a small JSON-safe diagnostic value."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_value_for_log(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _request_log_fields(ctx: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Select non-payload command arguments for operational diagnostics."""

    fields: dict[str, Any] = {}
    if "sources" in kwargs:
        fields["source_paths"] = _value_for_log(kwargs["sources"])
    for key in (
        "reference",
        "name",
        "plan_id",
        "group",
        "remap_group",
        "operation",
        "output",
        "tape_id",
        "media",
    ):
        value = kwargs.get(key)
        if value is not None:
            fields[key] = _value_for_log(value)

    # ``plan apply`` has no source argument, but the saved plan is still the
    # user's requested source set.  Loading it for diagnostics is best effort
    # and must never change command behavior or mask a catalog error.
    plan_id = kwargs.get("plan_id")
    if plan_id and "source_paths" not in fields:
        try:
            values = ctx.ensure_object(dict) if ctx is not None else {}
            config = values.get("config")
            if config is not None:
                plan = CatalogStore(config.catalog_root).load_plan(str(plan_id))
                fields["source_paths"] = _value_for_log(plan.source_paths)
        except (CatalogError, OSError, TypeError, ValueError):
            pass
    return fields


def _logger_for_context(ctx: Any) -> CommandLogger | None:
    if ctx is None:
        return None
    try:
        values = ctx.ensure_object(dict)
        logger = values.get("command_logger")
        if logger is None:
            config = values.get("config")
            if config is None:
                config = load_config()
                values["config"] = config
            command = getattr(ctx, "command_path", None) or "ltoctl"
            logger = CommandLogger(config.log_path, command)
            values["command_logger"] = logger
        return logger
    except (AttributeError, OSError, TypeError, ValueError):
        # Operational logging is deliberately non-canonical.
        return None


def _safe_typer_callback(callback: Any) -> Any:
    """Wrap a leaf command with logging, JSON diagnostics, and exit codes."""

    if getattr(callback, "_ltoctl_wrapped", False):
        return callback

    @functools.wraps(callback)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        if ctx is None:
            for value in args:
                if hasattr(value, "ensure_object") and hasattr(value, "command_path"):
                    ctx = value
                    break
        logger = _logger_for_context(ctx)
        request_fields = _request_log_fields(ctx, kwargs)
        if logger is not None:
            logger.start(**request_fields)
        machine = _json_mode(kwargs)
        try:
            value = callback(*args, **kwargs)
        except typer.Exit as exc:  # type: ignore[union-attr]
            if logger is not None:
                logger.finish(
                    ok=exc.exit_code == 0,
                    error=None if exc.exit_code == 0 else f"exit {exc.exit_code}",
                    **request_fields,
                )
            raise
        except (LtoctlError, ValueError, OSError) as exc:
            if logger is not None:
                logger.finish(ok=False, error=str(exc), **request_fields)
            if machine:
                _json_dump({"ok": False, "error": str(exc)})
            else:
                _typer_print(f"error: {exc}")
            raise typer.Exit(code=_exit_code_for_error(exc)) from exc  # type: ignore[union-attr]
        else:
            if logger is not None:
                finish_fields = dict(request_fields)
                finish_fields.update(result_fields(value))
                logger.finish(ok=True, **finish_fields)
            return value

    wrapped._ltoctl_wrapped = True
    return wrapped


def _instrument_typer_app(typer_app: Any) -> Any:
    """Install wrappers on commands in this app and all nested sub-apps."""

    if typer_app is None:
        return typer_app
    for command in getattr(typer_app, "registered_commands", ()):
        callback = getattr(command, "callback", None)
        if callback is not None:
            command.callback = _safe_typer_callback(callback)
    for group in getattr(typer_app, "registered_groups", ()):
        child = getattr(group, "typer_instance", None)
        if child is not None:
            _instrument_typer_app(child)
    return typer_app


def _json_dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _stream_search(results: Iterator[Any], *, json_mode: bool) -> None:
    """Render search results without exposing a partial JSON document.

    The TSV iterator can fail after yielding arbitrary rows (for example, a
    corrupt line near EOF).  Machine output therefore goes to a bounded
    ``SpooledTemporaryFile`` first; stdout receives the completed array only
    after the entire iterator succeeds.  The spool transparently moves to a
    temporary disk file after its memory threshold.
    """

    if not json_mode:
        sys.stdout.write("Tape\tFile\tArchive\tSize\tPath\n")
        sys.stdout.flush()
        for result in results:
            sys.stdout.write(
                f"{result.tape_id}\t{result.tape_file_no}\t{result.archive_name}\t"
                f"{format_bytes(result.size)}\t{result.path}\n"
            )
            sys.stdout.flush()
        return

    with tempfile.SpooledTemporaryFile(
        max_size=8 * 1024 * 1024,
        mode="w+",
        encoding="utf-8",
        newline="",
    ) as spool:
        spool.write("[")
        first = True
        for result in results:
            if not first:
                spool.write(",")
            spool.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
            first = False
        spool.write("]\n")
        spool.seek(0)
        shutil.copyfileobj(spool, sys.stdout, length=1024 * 1024)
        sys.stdout.flush()


def _typer_print(value: str) -> None:
    if _CONSOLE is None:
        print(value)
    else:
        _CONSOLE.print(value, markup=False, highlight=False)
    sys.stdout.flush()


def _echo_apply_progress(event: str, details: dict[str, object]) -> None:
    if event == "initialized":
        _typer_print(f"initialized {details['tape_id']} ({details['tape_uuid']})")
        return
    if event == "start_writing":
        _typer_print("start writing")
        return
    if event == "ejected":
        _typer_print("tape ejected")


def _plan_estimate_lines(plan: Any) -> list[str]:
    """Describe packed tape count without hiding oversized, unpackable units."""

    lines = [f"Estimated tapes: {plan.tape_count}"]
    if plan.oversized_units:
        lines.append(
            f"Oversized units not packed: {len(plan.oversized_units)}. "
            "They exceed usable capacity and are skipped at apply; "
            "plan them separately later, or split with --unit-depth or smaller sources."
        )
    return lines


def _human_plan(plan: Any) -> str:
    lines = [
        f"Plan: {plan.plan_id}",
        f"Media: {plan.media_type}",
        f"Usable capacity: {format_bytes(plan.usable_capacity_bytes)} per tape",
        f"Total source size: {format_bytes(plan.total_source_size_bytes)}",
        *_plan_estimate_lines(plan),
    ]
    for group in plan.groups:
        lines.append(
            f"\nTape {group.group_no:02d}  {format_bytes(group.used_bytes)} / "
            f"{format_bytes(group.capacity_bytes)}  ({group.fill_fraction * 100:.1f}%)"
        )
        for unit in group.units:
            lines.append(f"  {unit.name}  {format_bytes(unit.size_bytes)}")
    if plan.oversized_units:
        lines.append("\nOversized units:")
        for unit in plan.oversized_units:
            lines.append(f"  {unit.name}  {format_bytes(unit.size_bytes)}")
    if getattr(plan, "execution", None) is not None:
        lines.append(f"\nExecution: {plan.execution.status}")
        progress_by_group = {group.group_no: group for group in plan.execution.groups}
        for group in plan.groups:
            progress = progress_by_group[group.group_no]
            lines.append(
                f"  Group {group.group_no}: {progress.status} tape={progress.tape_id or 'unbound'} "
                f"completed={len(progress.completed_units)}/{len(group.units)}"
            )
    return "\n".join(lines)


def _human_apply(result: Any) -> str:
    lines = [f"Plan {result.plan_id}: {result.status}"]
    if result.group_no is not None:
        lines.append(f"Group: {result.group_no}")
    if result.bound_tape_id:
        lines.append(f"Tape: {result.bound_tape_id}")
    if getattr(result, "initialized_tape_id", None):
        lines.append(f"Initialized blank tape: {result.initialized_tape_id}")
    if result.applied_units:
        lines.append(f"Applied: {', '.join(result.applied_units)}")
    if result.recovered_units:
        lines.append(f"Recovered catalog commits: {', '.join(result.recovered_units)}")
    if result.skipped_units:
        lines.append(f"Already complete: {', '.join(result.skipped_units)}")
    if getattr(result, "oversized_units", None):
        lines.append(
            f"Oversized units left for a later plan: {', '.join(result.oversized_units)}"
        )
    return "\n".join(lines)


def _human_validation(report: Any) -> str:
    if report.ok:
        text = "Catalog OK"
    else:
        text = "Catalog invalid\n" + "\n".join(f"  ERROR: {error}" for error in report.errors)
    if report.warnings:
        text += "\n" + "\n".join(f"  WARNING: {warning}" for warning in report.warnings)
    return text


def _export_catalog(store: CatalogStore) -> str:
    lines = ["ltoctl catalog export", "", "Tapes:"]
    for tape in store.list_tapes():
        lines.append(f"  {tape.tape_id}  {tape.media_type}  {tape.status}  archives={len(tape.archives)}")
    lines.append("\nArchives:")
    for archive in store.list_archives():
        lines.append(
            f"  {archive.name}  tape={archive.tape_id}:{archive.tape_file_no}  "
            f"{format_bytes(archive.logical_size_bytes)}  {archive.status}"
        )
    return "\n".join(lines) + "\n"


def _linux_backend(device: str) -> LinuxTapeBackend:
    """Build the production Linux backend with the qualified close-only write path."""

    return LinuxTapeBackend(device, allow_unvalidated_write=True)


def _typer_store(ctx: Any) -> tuple[CatalogStore, Any]:
    values = ctx.ensure_object(dict)
    config = values.get("config")
    if config is None:
        config = load_config()
        values["config"] = config
    return CatalogStore(config.catalog_root), config


def _typer_plan_output(plan: Any, json_mode: bool) -> None:
    if json_mode:
        payload = plan.to_dict()
        payload["tape_count"] = plan.tape_count
        payload["total_source_size_bytes"] = plan.total_source_size_bytes
        payload["oversized_count"] = len(plan.oversized_units)
        _json_dump(payload)
        return
    if _CONSOLE is None:
        print(_human_plan(plan))
        return
    _CONSOLE.print(f"Plan: {plan.plan_id}", style="bold")
    _CONSOLE.print(f"Media: {plan.media_type}")
    _CONSOLE.print(f"Usable capacity: {format_bytes(plan.usable_capacity_bytes)} per tape")
    _CONSOLE.print(f"Total source size: {format_bytes(plan.total_source_size_bytes)}")
    for line in _plan_estimate_lines(plan):
        _CONSOLE.print(line)
    for group in plan.groups:
        table = Table(title=f"Tape {group.group_no:02d} — {group.fill_fraction * 100:.1f}%")
        table.add_column("Unit")
        table.add_column("Size", justify="right")
        for unit in group.units:
            table.add_row(unit.name, format_bytes(unit.size_bytes))
        _CONSOLE.print(table)
    if plan.oversized_units:
        table = Table(title="Oversized units")
        table.add_column("Unit")
        table.add_column("Size", justify="right")
        for unit in plan.oversized_units:
            table.add_row(unit.name, format_bytes(unit.size_bytes))
        _CONSOLE.print(table)
    if getattr(plan, "execution", None) is not None:
        _CONSOLE.print(f"Execution: {plan.execution.status}")
        table = Table(title="Execution groups")
        for column in ("Group", "Status", "Tape", "Completed"):
            table.add_column(column)
        progress_by_group = {group.group_no: group for group in plan.execution.groups}
        for group in plan.groups:
            progress = progress_by_group[group.group_no]
            table.add_row(
                str(group.group_no),
                progress.status,
                progress.tape_id or "unbound",
                f"{len(progress.completed_units)}/{len(group.units)}",
            )
        _CONSOLE.print(table)


def _build_typer_app() -> Any:
    """Construct the sole supported CLI command tree."""

    if not _HAS_TYPER:
        return None

    root = typer.Typer(name="ltoctl", help="Transparent personal LTO cold-archive tooling", no_args_is_help=True)
    drive_cli = typer.Typer(no_args_is_help=True, help="Inspect the tape drive")
    tape_cli = typer.Typer(no_args_is_help=True, help="Inspect cataloged tapes")
    archive_cli = typer.Typer(no_args_is_help=True, help="Inspect cataloged archives")
    plan_cli = typer.Typer(no_args_is_help=True, help="Plan archive units into tape groups")
    catalog_cli = typer.Typer(no_args_is_help=True, help="Maintain canonical metadata")
    verify_cli = typer.Typer(no_args_is_help=True, help="Verify catalog/tape data")
    root.add_typer(drive_cli, name="drive")
    root.add_typer(tape_cli, name="tape")
    root.add_typer(archive_cli, name="archive")
    root.add_typer(plan_cli, name="plan")
    root.add_typer(catalog_cli, name="catalog")
    root.add_typer(verify_cli, name="verify")

    @root.callback()
    def root_callback(
        ctx: typer.Context,
        catalog_root: Optional[Path] = typer.Option(
            None, "--catalog", help="catalog directory (default: ~/.local/share/ltoctl)"
        ),
        device: Optional[str] = typer.Option(None, "--device", help="non-rewinding tape device"),
    ) -> None:
        ctx.ensure_object(dict)
        ctx.obj["config"] = load_config(catalog_root=catalog_root, device=device)

    @drive_cli.command("status")
    def drive_status(
        ctx: typer.Context,
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        store, config = _typer_store(ctx)
        del store
        status = _linux_backend(config.device).status()
        if json_mode:
            _json_dump(status.to_dict())
        elif _CONSOLE is None:
            _typer_print(
                f"loaded={status.loaded} writable={status.writable} "
                f"file={status.file_no if status.file_no is not None else '?'}"
            )
        else:
            table = Table(title="Drive status")
            table.add_column("Field")
            table.add_column("Value")
            for field, value in status.to_dict().items():
                table.add_row(field, "" if value is None else str(value))
            _CONSOLE.print(table)
        if not status.ok:
            raise typer.Exit(code=3)
        return status

    @tape_cli.command("list")
    def tape_list(ctx: typer.Context, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        tapes = store.list_tapes()
        if json_mode:
            _json_dump([tape.to_dict() for tape in tapes])
        elif _CONSOLE is None:
            print("Tape ID\tMedia\tStatus\tArchives")
            for tape in tapes:
                print(f"{tape.tape_id}\t{tape.media_type}\t{tape.status}\t{len(tape.archives)}")
        else:
            table = Table(title="Tapes")
            for column in ("Tape ID", "Media", "Status", "Archives"):
                table.add_column(column)
            for tape in tapes:
                table.add_row(tape.tape_id, tape.media_type, tape.status, str(len(tape.archives)))
            _CONSOLE.print(table)
        return tapes

    @tape_cli.command("info")
    def tape_info(ctx: typer.Context, reference: str, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        tape = store.find_tape(reference)
        if json_mode:
            _json_dump(tape.to_dict())
        else:
            _typer_print(json.dumps(tape.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return tape

    @tape_cli.command("init")
    def tape_init_command(
        ctx: typer.Context,
        tape_id: str,
        media: Optional[str] = typer.Option(None, "--media"),
        yes: bool = typer.Option(False, "--yes", help="confirm destructive initialization"),
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        if not yes:
            if json_mode:
                _json_dump({"ok": False, "error": "tape initialization requires --yes"})
                raise typer.Exit(code=2)
            yes = typer.confirm(
                f"Initialize tape {tape_id}? This is destructive and requires a blank tape.",
                default=False,
                abort=False,
            )
            if not yes:
                raise typer.Exit(code=2)
        store, config = _typer_store(ctx)
        result = init_tape(
            store,
            _linux_backend(config.device),
            tape_id,
            media=media or config.media,
            confirm=True,
        )
        if json_mode:
            _json_dump({"tape": result.tape.to_dict(), "operation_uuid": result.operation_uuid})
        else:
            _typer_print(f"initialized {result.tape.tape_id} ({result.tape.uuid})")
        return result

    @tape_cli.command("reconcile")
    def tape_reconcile_command(
        ctx: typer.Context,
        operation: Optional[str] = typer.Option(None, "--operation"),
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        store, config = _typer_store(ctx)
        result = reconcile_tape(store, _linux_backend(config.device), operation_uuid=operation)
        if json_mode:
            _json_dump(result.to_dict())
        else:
            _typer_print(result.messages[-1] if result.messages else "tape reconciled")
        if not result.ok:
            raise typer.Exit(code=4)
        return result

    @tape_cli.command("eject")
    def tape_eject_command(
        ctx: typer.Context,
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        _, config = _typer_store(ctx)
        _linux_backend(config.device).eject()
        if json_mode:
            _json_dump({"ok": True, "message": "tape ejected"})
        else:
            _typer_print("tape ejected")
        return {"ok": True}

    @archive_cli.command("list")
    def archive_list(ctx: typer.Context, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        archives = store.list_archives()
        if json_mode:
            _json_dump([archive.to_dict() for archive in archives])
        elif _CONSOLE is None:
            print("Tape\tFile\tArchive\tSize\tStatus")
            for archive in archives:
                print(
                    f"{archive.tape_id}\t{archive.tape_file_no}\t{archive.name}\t"
                    f"{format_bytes(archive.logical_size_bytes)}\t{archive.status}"
                )
        else:
            table = Table(title="Archives")
            for column in ("Tape", "File", "Archive", "Size", "Status"):
                table.add_column(column)
            for archive in archives:
                table.add_row(
                    archive.tape_id,
                    str(archive.tape_file_no),
                    archive.name,
                    format_bytes(archive.logical_size_bytes),
                    archive.status,
                )
            _CONSOLE.print(table)
        return archives

    @archive_cli.command("add")
    def archive_add_command(
        ctx: typer.Context,
        sources: list[Path] = typer.Argument(..., help="source files/directories"),
        name: str = typer.Option(..., "--name"),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        archive = add_archive(store, _linux_backend(config.device), sources, name=name)
        if json_mode:
            _json_dump(archive.to_dict())
        else:
            _typer_print(f"{archive.name}\t{archive.tape_id}:{archive.tape_file_no}\t{archive.archive_uuid}")
        return archive

    @archive_cli.command("mark-obsolete")
    def archive_mark_obsolete_command(
        ctx: typer.Context,
        reference: str,
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, _ = _typer_store(ctx)
        archive = mark_obsolete(store, reference)
        if json_mode:
            _json_dump(archive.to_dict())
        else:
            _typer_print(f"{archive.name}\t{archive.archive_uuid}\t{archive.status}")
        return archive

    @plan_cli.command("create")
    def plan_create(
        ctx: typer.Context,
        sources: list[Path] = typer.Argument(..., help="source files/directories"),
        media: Optional[str] = typer.Option(None, "--media"),
        capacity: Optional[str] = typer.Option(None, "--capacity"),
        unit_depth: Optional[int] = typer.Option(None, "--unit-depth"),
        save: Optional[str] = typer.Option(None, "--save", metavar="PLAN_ID"),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        plan = plan_sources(
            sources,
            media=media or config.media,
            capacity=capacity,
            unit_depth=unit_depth,
            plan_id=save,
        )
        if save:
            store.save_plan(plan)
        _typer_plan_output(plan, json_mode)
        return plan

    @plan_cli.command("show")
    def plan_show(ctx: typer.Context, plan_id: str, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        plan = store.load_plan(plan_id)
        _typer_plan_output(plan, json_mode)
        return plan

    @plan_cli.command("list")
    def plan_list(ctx: typer.Context, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        plans = store.list_plans()
        if json_mode:
            _json_dump([plan.to_dict() for plan in plans])
        elif _CONSOLE is None:
            for plan in plans:
                print(f"{plan.plan_id}\t{plan.media_type}\t{plan.tape_count} tapes\t{format_bytes(plan.total_source_size_bytes)}")
        else:
            table = Table(title="Plans")
            for column in ("Plan", "Media", "Tapes", "Source size"):
                table.add_column(column)
            for plan in plans:
                table.add_row(plan.plan_id, plan.media_type, str(plan.tape_count), format_bytes(plan.total_source_size_bytes))
            _CONSOLE.print(table)
        return plans

    @plan_cli.command("apply")
    def plan_apply_command(
        ctx: typer.Context,
        plan_id: str,
        group: Optional[int] = typer.Option(None, "--group", help="execute only this group without prompting"),
        remap_group: Optional[int] = typer.Option(None, "--remap-group", help="explicitly rebind an unfinished group"),
        init_tape: Optional[str] = typer.Option(
            None,
            "--init-tape",
            help="initialize a blank loaded cartridge with this tape ID during apply",
        ),
        yes: bool = typer.Option(
            False, "--yes", "--confirm", help="confirm a group remap or blank-tape initialization"
        ),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        complete_json_noop = False
        if remap_group is not None and not yes:
            if json_mode:
                _json_dump({"ok": False, "error": "--remap-group requires --yes with --json"})
                raise typer.Exit(code=2)
            yes = typer.confirm(
                f"Remap plan group {remap_group}? This changes its tape binding.",
                default=False,
                abort=False,
            )
            if not yes:
                raise typer.Exit(code=2)

        if init_tape is not None and json_mode and not yes:
            _json_dump({"ok": False, "error": "--init-tape requires --yes with --json"})
            raise typer.Exit(code=2)

        if json_mode and group is None:
            try:
                saved_plan = store.load_plan(plan_id)
            except LtoctlError as exc:
                _json_dump({"ok": False, "error": str(exc)})
                raise typer.Exit(code=_exit_code_for_error(exc))
            if remap_group is not None or saved_plan.execution.status != "complete":
                _json_dump(
                    {
                        "ok": False,
                        "error": "--json plan apply requires an explicit --group unless the plan is already complete",
                    }
                )
                raise typer.Exit(code=2)
            complete_json_noop = True

        before_group = None
        if not json_mode and group is None:
            def before_group(group_record: Any, execution: Any) -> None:
                expected = execution.tape_id or "a blank or initialized tape"
                _typer_print(
                    f"Plan {plan_id}: group {group_record.group_no}; load {expected} and press Enter to continue."
                )
                input()

        confirm_init = bool(init_tape) and yes
        on_blank_tape = None
        if not json_mode and not confirm_init:
            def on_blank_tape(group_record: Any, execution: Any) -> str:
                suggested = (init_tape or "").strip() or None
                _typer_print(
                    f"Plan {plan_id} group {group_record.group_no}: "
                    "loaded cartridge is blank (no identity header)."
                )
                if suggested:
                    if not typer.confirm(
                        f"Initialize tape {suggested}? This is destructive and requires a blank tape.",
                        default=False,
                        abort=False,
                    ):
                        raise typer.Exit(code=2)
                    return suggested
                if not typer.confirm(
                    "Initialize this tape now so the plan group can be written?",
                    default=False,
                    abort=False,
                ):
                    raise typer.Exit(code=2)
                tape_id = str(typer.prompt("Tape ID to write on the label")).strip()
                if not tape_id:
                    raise typer.Exit(code=2)
                if not typer.confirm(
                    f"Initialize tape {tape_id}? This is destructive and requires a blank tape.",
                    default=False,
                    abort=False,
                ):
                    raise typer.Exit(code=2)
                return tape_id

        try:
            result = apply_plan(
                store,
                None if complete_json_noop else _linux_backend(config.device),
                plan_id,
                group_no=group,
                before_group=before_group,
                remap_group=remap_group,
                confirm_remap=yes,
                init_tape_id=init_tape,
                confirm_init=confirm_init,
                on_blank_tape=on_blank_tape,
                on_progress=None if json_mode else _echo_apply_progress,
            )
        except PlanSourceDriftError as exc:
            if json_mode:
                _json_dump(exc.to_dict())
                raise typer.Exit(code=2)
            raise
        except LtoctlError as exc:
            if json_mode:
                _json_dump({"ok": False, "error": str(exc)})
                raise typer.Exit(code=_exit_code_for_error(exc))
            raise
        if json_mode:
            _json_dump(result.to_dict())
        else:
            _typer_print(_human_apply(result))
        return result

    @catalog_cli.command("validate")
    def catalog_validate(ctx: typer.Context, json_mode: bool = typer.Option(False, "--json")) -> Any:
        store, _ = _typer_store(ctx)
        report = validate_catalog(store)
        if json_mode:
            _json_dump(report.to_dict())
        else:
            _typer_print(_human_validation(report))
        if not report.ok:
            raise typer.Exit(code=2)
        return report

    @catalog_cli.command("rebuild-index")
    def catalog_rebuild_index(
        ctx: typer.Context,
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        store, _ = _typer_store(ctx)
        path = rebuild_index(store)
        if json_mode:
            _json_dump({"ok": True, "path": str(path)})
        else:
            _typer_print(str(path))
        return path

    @catalog_cli.command("export")
    def catalog_export(
        ctx: typer.Context,
        output: Optional[Path] = typer.Option(None, "--output"),
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        store, _ = _typer_store(ctx)
        text = _export_catalog(store)
        if output:
            atomic_write_text(output, text)
            if json_mode:
                _json_dump({"ok": True, "path": str(output)})
            else:
                _typer_print(str(output))
            return output
        if json_mode:
            _json_dump(
                {
                    "tapes": [tape.to_dict() for tape in store.list_tapes()],
                    "archives": [archive.to_dict() for archive in store.list_archives()],
                }
            )
        else:
            _typer_print(text.rstrip("\n"))
        return text

    @verify_cli.command("catalog")
    def verify_catalog_command(
        ctx: typer.Context,
        json_mode: bool = typer.Option(False, "--json", help="emit one JSON value"),
    ) -> Any:
        store, _ = _typer_store(ctx)
        report = validate_catalog(store)
        if json_mode:
            _json_dump(report.to_dict())
        else:
            _typer_print(_human_validation(report))
        if not report.ok:
            raise typer.Exit(code=2)
        return report

    @root.command("search")
    def search_command(
        ctx: typer.Context,
        query: str,
        exact: bool = typer.Option(False, "--exact"),
        regex: bool = typer.Option(False, "--regex"),
        tape: Optional[str] = typer.Option(None, "--tape"),
        archive: Optional[str] = typer.Option(None, "--archive"),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> None:
        store, _ = _typer_store(ctx)
        _stream_search(
            search_index(
                store.root / "index" / "files.tsv",
                query,
                exact=exact,
                regex=regex,
                tape=tape,
                archive=archive,
            ),
            json_mode=json_mode,
        )

    @root.command("restore")
    def restore_command(
        ctx: typer.Context,
        reference: str,
        output: Path = typer.Option(..., "--output"),
        selected_files: Optional[list[str]] = typer.Option(None, "--file"),
        overwrite: bool = typer.Option(False, "--overwrite"),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        result = restore_archive(
            store,
            _linux_backend(config.device),
            reference,
            output,
            selected_files=selected_files,
            overwrite=overwrite,
        )
        if json_mode:
            payload: dict[str, Any] = {
                "archive_uuid": result.archive_uuid,
                "output": result.output,
                "extracted_count": result.extracted_count,
            }
            if result.extracted is not None:
                payload["extracted"] = list(result.extracted)
            _json_dump(payload)
        else:
            _typer_print(f"restored {result.extracted_count} entries to {result.output}")
        return result

    @verify_cli.command("archive")
    def verify_archive_command(
        ctx: typer.Context,
        reference: str,
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        result = verify_archive(store, _linux_backend(config.device), reference)
        if json_mode:
            _json_dump(result.to_dict())
        else:
            _typer_print(f"{result.archive_name}: {'OK' if result.ok else 'MISMATCH'}")
            if result.error:
                _typer_print(f"  {result.error}")
        if not result.ok:
            raise typer.Exit(code=4)
        return result

    @verify_cli.command("tape")
    def verify_tape_command(
        ctx: typer.Context,
        reference: str,
        json_mode: bool = typer.Option(False, "--json"),
    ) -> Any:
        store, config = _typer_store(ctx)
        results = verify_tape(store, _linux_backend(config.device), reference)
        if json_mode:
            _json_dump([result.to_dict() for result in results])
        else:
            for result in results:
                _typer_print(f"{result.tape_file_no} {result.archive_name}: {'OK' if result.ok else 'MISMATCH'}")
        if not all(result.ok for result in results):
            raise typer.Exit(code=4)
        return results

    return _instrument_typer_app(root)


app = _build_typer_app()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the formal Typer CLI and return its process status."""

    if app is None:
        print(
            "ltoctl: the formal CLI requires Typer and Rich; install with "
            "python -m pip install 'ltoctl[dev]' (or install the project dependencies)",
            file=sys.stderr,
        )
        return 2
    try:
        values = list(argv) if argv is not None else None
        result = app(args=values, standalone_mode=False)
        return result if isinstance(result, int) else 0
    except typer.Exit as exc:  # type: ignore[union-attr]
        return exc.exit_code
    except (LtoctlError, ValueError, OSError) as exc:
        print(f"ltoctl: error: {exc}", file=sys.stderr)
        return _exit_code_for_error(exc)
    except Exception as exc:
        if _is_no_args_is_help(exc):
            _print_group_help(exc)
            return 2
        if click is not None and isinstance(exc, click.Abort):
            print("ltoctl: aborted", file=sys.stderr)
            return 2
        if click is not None and isinstance(exc, click.ClickException):
            print(f"ltoctl: error: {exc}", file=sys.stderr)
            return 2
        raise
