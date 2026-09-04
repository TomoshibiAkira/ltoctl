"""Phase 4 presentation and catalog-maintenance regression tests.

The Typer tests are intentionally skipped in the stdlib-only development
environment. Once the declared dependencies are installed, the same tests
run under unittest or pytest.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ltoctl.catalog.models import ArchiveRecord, TapeRecord
from ltoctl.catalog.store import CatalogStore
from ltoctl.catalog.validation import validate_catalog
from ltoctl.command_logging import CommandLogger
from ltoctl.errors import ArchiveError, PlannerError, SafetyError, TapeError, VerificationError
from ltoctl.planner.scanner import plan_sources

try:
    import rich  # noqa: F401
    import typer  # noqa: F401
    from typer.testing import CliRunner

    HAS_TYPER_RICH = True
except ImportError:  # pragma: no cover - dependency-gated tests
    HAS_TYPER_RICH = False
    CliRunner = None  # type: ignore[assignment,misc]


class Phase4CatalogAndLoggingTests(unittest.TestCase):
    def test_command_logger_is_append_only_and_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "ltoctl.log"
            logger = CommandLogger(path, "ltoctl catalog validate")
            logger.start()
            logger.start()
            logger.finish(ok=True, tape_id="T-1")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["result"] for line in lines], ["start", "ok"])
            self.assertEqual(json.loads(lines[-1])["tape_id"], "T-1")

            # An unwritable destination is diagnostic-only and must not raise.
            unavailable = CommandLogger(Path(temporary) / "not-a-file" / "log", "x")
            Path(temporary, "not-a-file").write_text("occupied", encoding="utf-8")
            unavailable.event("ok")

    def test_catalog_validation_reports_manifest_and_relationship_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            tape = TapeRecord.new("T-1")
            store.save_tape(tape)
            archive = ArchiveRecord(
                "a-1",
                "one",
                tape.tape_id,
                tape.uuid,
                1,
                source_paths=["/source"],
                logical_size_bytes=3,
                file_count=1,
            )
            store.save_archive(archive)
            store.save_manifest(
                archive.archive_uuid,
                [{"path": "one/file", "size": 3, "mtime_ns": 1, "type": "file"}],
            )
            tape.archives.append(archive.archive_uuid)
            store.save_tape(tape)
            self.assertTrue(validate_catalog(store).ok)

            # Keep the record schema valid but break the physical reverse
            # relation; validation must report it rather than silently repair.
            tape.archives.clear()
            store.save_tape(tape)
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("does not reference archive" in error for error in report.errors))

            # A manifest can be JSONL-valid yet disagree with its snapshot.
            tape.archives.append(archive.archive_uuid)
            store.save_tape(tape)
            store.save_manifest(
                archive.archive_uuid,
                [{"path": "one/file", "size": 2, "mtime_ns": 1, "type": "file"}],
            )
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("logical size" in error for error in report.errors))

    def test_catalog_validation_matrix_reports_status_and_file_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            tape = TapeRecord.new("T-1")
            store.save_tape(tape)
            first = ArchiveRecord("a-1", "one", tape.tape_id, tape.uuid, 1, file_count=0)
            second = ArchiveRecord("a-2", "two", tape.tape_id, tape.uuid, 1, file_count=0)
            store.save_archive(first)
            store.save_archive(second)
            store.save_manifest(first.archive_uuid, [])
            store.save_manifest(second.archive_uuid, [])
            tape.archives[:] = [first.archive_uuid, second.archive_uuid]
            store.save_tape(tape)
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("collides" in error for error in report.errors))

            corrupted = json.loads((store.root / "tapes" / "T-1.json").read_text(encoding="utf-8"))
            corrupted["status"] = "not-a-real-status"
            (store.root / "tapes" / "T-1.json").write_text(
                json.dumps(corrupted), encoding="utf-8"
            )
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("cannot load tapes" in error for error in report.errors))

    def test_catalog_validation_uses_streaming_manifest_order_and_filename_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            tape = TapeRecord.new("T-1")
            store.save_tape(tape)
            archive = ArchiveRecord("a-1", "ordered", tape.tape_id, tape.uuid, 1)
            store.save_archive(archive)
            entries = [
                {"path": f"ordered-{index:05d}", "size": 0, "mtime_ns": index, "type": "dir"}
                for index in range(2000)
            ]
            store.save_manifest(archive.archive_uuid, entries)
            tape.archives.append(archive.archive_uuid)
            store.save_tape(tape)
            self.assertTrue(validate_catalog(store).ok)

            # Strictly increasing order detects a duplicate without retaining
            # every path in a set.
            entries[1500] = dict(entries[1499])
            store.save_manifest(archive.archive_uuid, entries)
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("strictly lexical" in error for error in report.errors))

            # A record copied under a different filename is an identity error,
            # not an additional valid catalog object.
            tape_path = store.root / "tapes" / "T-1.json"
            (store.root / "tapes" / "wrong-name.json").write_text(
                tape_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            report = validate_catalog(store)
            self.assertFalse(report.ok)
            self.assertTrue(any("cannot load tapes" in error for error in report.errors))

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_confirmation_and_json_error_atomicity(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            catalog = str(Path(temporary) / "catalog")
            runner = CliRunner()
            with patch("ltoctl.cli.init_tape") as initialize:
                human = runner.invoke(
                    app,
                    ["--catalog", catalog, "tape", "init", "T-1"],
                    input="n\n",
                )
                self.assertEqual(human.exit_code, 2, human.stdout)
                self.assertIn("Initialize tape", human.stdout)
                initialize.assert_not_called()

            machine = runner.invoke(
                app,
                ["--catalog", catalog, "tape", "init", "T-1", "--json"],
            )
            self.assertEqual(machine.exit_code, 2, machine.stdout)
            self.assertEqual(json.loads(machine.stdout)["ok"], False)

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_exit_code_matrix(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            catalog = str(Path(temporary) / "catalog")
            runner = CliRunner()
            cases = (
                ("tape init safety", ["tape", "init", "T-1", "--yes", "--json"], SafetyError("unsafe"), 3),
                ("archive add tape", ["archive", "add", "source", "--name", "a", "--json"], TapeError("io"), 4),
                ("archive add archive", ["archive", "add", "source", "--name", "a", "--json"], ArchiveError("bad"), 4),
                ("verify archive", ["verify", "archive", "a", "--json"], VerificationError("bad"), 4),
                ("plan create planner", ["plan", "create", "source", "--json"], PlannerError("bad"), 2),
            )
            for label, args, failure, expected_code in cases:
                with self.subTest(label=label):
                    target = {
                        "tape init safety": "ltoctl.cli.init_tape",
                        "archive add tape": "ltoctl.cli.add_archive",
                        "archive add archive": "ltoctl.cli.add_archive",
                        "verify archive": "ltoctl.cli.verify_archive",
                        "plan create planner": "ltoctl.cli.plan_sources",
                    }[label]
                    with patch(target, side_effect=failure):
                        result = runner.invoke(app, ["--catalog", catalog, *args])
                    self.assertEqual(result.exit_code, expected_code, result.stdout)
                    self.assertIsInstance(json.loads(result.stdout), dict)

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_search_json_is_atomic_on_missing_or_bad_tsv(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = CatalogStore(root / "catalog")
            runner = CliRunner()
            missing = runner.invoke(
                app,
                ["--catalog", str(catalog.root), "search", "needle", "--json"],
            )
            self.assertEqual(missing.exit_code, 2, missing.stdout)
            self.assertEqual(json.loads(missing.stdout)["ok"], False)
            index = catalog.root / "index" / "files.tsv"
            index.write_text(
                "tape_id\ttape_file_no\tarchive_uuid\tarchive_name\tsize\tmtime_ns\tpath\n"
                "T\t1\ta\tA\t1\t1\tgood\n"
                "corrupt\trow\n",
                encoding="utf-8",
            )
            bad = runner.invoke(app, ["--catalog", str(catalog.root), "search", "good", "--json"])
            self.assertEqual(bad.exit_code, 2, bad.stdout)
            payload = json.loads(bad.stdout)
            self.assertIsInstance(payload, dict)
            self.assertFalse(payload["ok"])

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_logs_source_arguments_for_archive_and_plan(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_text("payload", encoding="utf-8")
            catalog = CatalogStore(root / "catalog")
            log_path = root / "commands.jsonl"
            runner = CliRunner()
            with patch.dict("os.environ", {"LTOCTL_LOG": str(log_path)}, clear=False):
                with patch("ltoctl.cli.add_archive", side_effect=ArchiveError("blocked")):
                    archive_result = runner.invoke(
                        app,
                        ["--catalog", str(catalog.root), "archive", "add", str(source), "--name", "a", "--json"],
                    )
                with patch("ltoctl.cli.plan_sources", side_effect=PlannerError("blocked")):
                    plan_result = runner.invoke(
                        app,
                        ["--catalog", str(catalog.root), "plan", "create", str(source), "--json"],
                    )
                saved_plan = plan_sources([source], capacity="8B", plan_id="logged-plan")
                catalog.save_plan(saved_plan)
                shown = runner.invoke(
                    app,
                    ["--catalog", str(catalog.root), "plan", "show", "logged-plan", "--json"],
                )
            self.assertEqual(archive_result.exit_code, 4, archive_result.stdout)
            self.assertEqual(plan_result.exit_code, 2, plan_result.stdout)
            self.assertEqual(shown.exit_code, 0, shown.stdout)
            self.assertEqual(json.loads(shown.stdout)["plan_id"], "logged-plan")
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            starts = [event for event in events if event["result"] == "start"]
            self.assertGreaterEqual(len(starts), 2)
            self.assertTrue(any(str(source) in event.get("source_paths", []) for event in starts))

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_json_smoke_and_stable_error_code(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            catalog = str(Path(temporary) / "catalog")
            runner = CliRunner()
            listed = runner.invoke(app, ["--catalog", catalog, "tape", "list", "--json"])
            self.assertEqual(listed.exit_code, 0, listed.stdout)
            self.assertEqual(json.loads(listed.stdout), [])

            missing = runner.invoke(
                app,
                ["--catalog", catalog, "archive", "mark-obsolete", "missing", "--json"],
            )
            self.assertEqual(missing.exit_code, 2, missing.stdout)
            self.assertIsInstance(json.loads(missing.stdout), dict)

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_json_logs_without_stdout_contamination(self) -> None:
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            catalog = str(Path(temporary) / "catalog")
            log_path = str(Path(temporary) / "commands.jsonl")
            runner = CliRunner()
            with patch.dict("os.environ", {"LTOCTL_LOG": log_path}, clear=False):
                result = runner.invoke(app, ["--catalog", catalog, "tape", "list", "--json"])
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout), [])
            events = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["result"] for event in events], ["start", "ok"])
            self.assertIn("tape list", events[0]["command"])

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_main_no_args_prints_group_help_and_exits_2(self) -> None:
        from ltoctl.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([])
        self.assertEqual(code, 2)
        text = stdout.getvalue() + stderr.getvalue()
        self.assertIn("Usage:", text)
        self.assertNotIn("ltoctl: error:", text)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["tape"])
        self.assertEqual(code, 2)
        text = stdout.getvalue() + stderr.getvalue()
        self.assertIn("Usage:", text)
        self.assertIn("init", text)
        self.assertNotIn("ltoctl: error:", text)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--help"])
        self.assertEqual(code, 0)
        self.assertNotIn("ltoctl: error:", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
