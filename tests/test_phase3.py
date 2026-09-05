from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ltoctl.archive.obsolete import mark_obsolete
from ltoctl.archive.writer import add_archive
from ltoctl.catalog.models import ArchiveRecord, PlanRecord, derive_plan_archive_uuid
from ltoctl.catalog.store import CatalogStore
from ltoctl.errors import CatalogError, CatalogValidationError, PlannerError, SafetyError
from ltoctl.planner.executor import PlanSourceDriftError, apply_plan
from ltoctl.planner.scanner import plan_sources
from ltoctl.tape.mock import MockTapeBackend
from ltoctl.tape.service import init_tape

try:
    import rich  # noqa: F401
    import typer  # noqa: F401
    from typer.testing import CliRunner

    HAS_TYPER_RICH = True
except ImportError:  # pragma: no cover - optional UI dependency
    HAS_TYPER_RICH = False
    CliRunner = None  # type: ignore[assignment,misc]


class FailingPlanStore(CatalogStore):
    """Fail once after a physical archive has committed."""

    def __init__(self, root: Path):
        self.fail_after_archive = False
        super().__init__(root)

    def save_plan(self, record):
        if self.fail_after_archive and any(
            group.completed_units for group in record.execution.groups
        ):
            self.fail_after_archive = False
            raise OSError("simulated plan progress save failure")
        return super().save_plan(record)


class Phase3Tests(unittest.TestCase):
    def _plan_sources(self, root: Path, *, capacity: str = "4B") -> tuple[Path, Path]:
        first = root / "first"
        second = root / "second"
        first.write_bytes(b"1" * 3)
        second.write_bytes(b"2" * 3)
        return first, second

    def test_typed_execution_roundtrip_and_malicious_progress_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root)
            plan = plan_sources([first, second], capacity="4B", plan_id="typed")
            data = plan.to_dict()
            self.assertEqual(data["execution"]["status"], "planned")
            restored = PlanRecord.from_dict(data)
            self.assertEqual(restored.execution.status, "planned")
            self.assertEqual([g.status for g in restored.execution.groups], ["pending", "pending"])

            corruptions = []
            unknown = deepcopy(data)
            unknown["execution"]["status"] = "in_progress"
            unknown["execution"]["groups"][0]["status"] = "in_progress"
            unknown["execution"]["groups"][0]["tape_id"] = "T"
            unknown["execution"]["groups"][0]["tape_uuid"] = "U"
            unknown["execution"]["groups"][0]["completed_units"] = {"not-a-unit": "archive"}
            corruptions.append(unknown)

            missing_archive = deepcopy(data)
            missing_archive["execution"]["status"] = "in_progress"
            missing_archive["execution"]["groups"][0].update(
                {"status": "in_progress", "tape_id": "T", "tape_uuid": "U", "completed_units": {"x": ""}}
            )
            corruptions.append(missing_archive)

            pending_binding = deepcopy(data)
            pending_binding["execution"]["groups"][0].update(
                {"tape_id": "T", "tape_uuid": "U"}
            )
            corruptions.append(pending_binding)

            duplicate_groups = deepcopy(data)
            duplicate_groups["execution"]["groups"].append(
                deepcopy(duplicate_groups["execution"]["groups"][0])
            )
            corruptions.append(duplicate_groups)

            arbitrary = deepcopy(data)
            arbitrary["execution"] = {
                "schema_version": 1,
                "status": "planned",
                "groups": deepcopy(data["execution"]["groups"]),
                "unexpected": True,
            }
            corruptions.append(arbitrary)

            for value in corruptions:
                with self.subTest(value=value), self.assertRaises(CatalogValidationError):
                    PlanRecord.from_dict(value)

    def test_execution_sequence_and_tape_uuid_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root, capacity="4B")
            plan = plan_sources([first, second], capacity="4B", plan_id="sequence")

            pending_then_active = plan.to_dict()
            pending_then_active["execution"]["status"] = "in_progress"
            pending_then_active["execution"]["groups"][1].update(
                {"status": "in_progress", "tape_id": "T", "tape_uuid": "U"}
            )
            with self.assertRaises(CatalogValidationError):
                PlanRecord.from_dict(pending_then_active)

            duplicate_tape = plan.to_dict()
            duplicate_tape["execution"]["status"] = "in_progress"
            first_group, second_group = duplicate_tape["execution"]["groups"]
            first_key = plan.groups[0].units[0].unit_id
            first_group.update(
                {
                    "status": "complete",
                    "tape_id": "T",
                    "tape_uuid": "same-uuid",
                    "completed_units": {first_key: "archive-one"},
                }
            )
            second_group.update(
                {"status": "in_progress", "tape_id": "T2", "tape_uuid": "same-uuid"}
            )
            with self.assertRaises(CatalogValidationError):
                PlanRecord.from_dict(duplicate_tape)

    def test_guided_multi_tape_apply_binds_groups_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root)
            store = CatalogStore(root / "catalog")
            tape_one = MockTapeBackend()
            tape_two = MockTapeBackend()
            init_tape(store, tape_one, "T-ONE", confirm=True)
            init_tape(store, tape_two, "T-TWO", confirm=True)
            plan = plan_sources([first, second], capacity="4B", plan_id="guided")
            store.save_plan(plan)
            seen: list[int] = []

            def before_group(group):
                seen.append(group.group_no)
                return tape_one if group.group_no == 1 else tape_two

            events: list[str] = []
            result = apply_plan(
                store,
                tape_one,
                "guided",
                before_group=before_group,
                on_progress=lambda event, details, events=events: events.append(event),
            )
            saved = store.load_plan("guided")
            self.assertEqual(result.status, "complete")
            self.assertEqual(seen, [1, 2])
            self.assertEqual(events, ["start_writing", "ejected", "start_writing"])
            self.assertEqual(saved.execution.status, "complete")
            self.assertEqual([g.status for g in saved.execution.groups], ["complete", "complete"])
            self.assertEqual(len(tape_one.files), 2)
            self.assertEqual(len(tape_two.files), 2)
            archives = store.list_archives()
            self.assertEqual({archive.tape_id for archive in archives}, {"T-ONE", "T-TWO"})
            self.assertFalse(tape_one.loaded)
            self.assertTrue(tape_two.loaded)

    def test_group_selection_requires_previous_completion_and_wrong_tape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root)
            store = CatalogStore(root / "catalog")
            tape_one = MockTapeBackend()
            tape_two = MockTapeBackend()
            init_tape(store, tape_one, "T-ONE", confirm=True)
            init_tape(store, tape_two, "T-TWO", confirm=True)
            plan = plan_sources([first, second], capacity="4B", plan_id="ordered")
            store.save_plan(plan)
            with self.assertRaises(PlannerError):
                apply_plan(store, tape_one, "ordered", group_no=2)
            apply_plan(store, tape_one, "ordered", group_no=1)
            self.assertFalse(tape_one.loaded)
            saved = store.load_plan("ordered")
            second_execution = saved.execution.groups[1]
            second_execution.status = "in_progress"
            second_execution.tape_id = "T-TWO"
            second_execution.tape_uuid = store.find_tape("T-TWO").uuid
            saved.execution.status = "in_progress"
            store.save_plan(saved)
            with self.assertRaises(SafetyError):
                apply_plan(store, tape_one, "ordered", group_no=2)
            result = apply_plan(store, tape_two, "ordered", group_no=2)
            self.assertEqual(result.status, "complete")
            self.assertTrue(tape_two.loaded)
            no_op = apply_plan(store, tape_two, "ordered", group_no=2)
            self.assertEqual(no_op.skipped_units, [str(second)])

    def test_plan_save_failure_recovers_committed_archive_without_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = FailingPlanStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            tape = store.find_tape("T-ONE")
            tape.recommended_capacity_bytes = 3
            store.save_tape(tape)
            plan = plan_sources([first], capacity="8B", plan_id="recover")
            store.save_plan(plan)
            store.fail_after_archive = True
            with self.assertRaises(OSError):
                apply_plan(store, backend, "recover", group_no=1)
            self.assertEqual(len(backend.files), 2)
            self.assertEqual(len(store.list_archives()), 1)
            stale = store.load_plan("recover")
            self.assertEqual(stale.execution.groups[0].completed_units, {})

            result = apply_plan(store, backend, "recover", group_no=1)
            self.assertEqual(result.recovered_units, [str(first)])
            self.assertEqual(result.applied_units, [])
            self.assertEqual(len(backend.files), 2)
            recovered = store.load_plan("recover")
            self.assertEqual(recovered.execution.status, "complete")
            expected_uuid = derive_plan_archive_uuid(
                recovered.plan_id,
                recovered.created_at,
                1,
                recovered.groups[0].units[0].unit_id,
            )
            self.assertEqual(recovered.execution.groups[0].completed_units[str(first)], expected_uuid)

    def test_plan_recovery_never_adopts_unrelated_or_conflicting_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="identity")
            store.save_plan(plan)

            # Same source metadata on another UUID is not a plan completion;
            # it must never be adopted as a recovery candidate.
            unrelated = add_archive(store, backend, [first], name="unrelated-copy")
            result = apply_plan(store, backend, "identity", group_no=1)
            self.assertEqual(result.recovered_units, [])
            self.assertEqual(result.applied_units, [str(first)])
            expected_uuid = derive_plan_archive_uuid(
                plan.plan_id,
                plan.created_at,
                1,
                plan.groups[0].units[0].unit_id,
            )
            self.assertNotEqual(unrelated.archive_uuid, expected_uuid)
            self.assertEqual(store.load_archive(expected_uuid).archive_uuid, expected_uuid)

            # A record at the exact deterministic UUID is considered only if
            # every identity field is valid; a conflicting record blocks
            # execution rather than being silently repaired or guessed around.
            conflict_plan = plan_sources([first], capacity="8B", plan_id="conflict")
            store.save_plan(conflict_plan)
            conflict_uuid = derive_plan_archive_uuid(
                conflict_plan.plan_id,
                conflict_plan.created_at,
                1,
                conflict_plan.groups[0].units[0].unit_id,
            )
            fake = ArchiveRecord(
                archive_uuid=conflict_uuid,
                name="wrong-name",
                tape_id="T-ONE",
                tape_uuid=store.find_tape("T-ONE").uuid,
                tape_file_no=3,
                source_paths=[str(first)],
                logical_size_bytes=first.stat().st_size,
                file_count=1,
                tar_stream_sha256="fake-hash",
                status="active",
            )
            store.save_archive(fake)
            with self.assertRaises(PlannerError):
                apply_plan(store, backend, "conflict", group_no=1)
            self.assertEqual(len(backend.files), 3)

    def test_group_capacity_preflight_rejects_whole_group_before_any_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            tape = store.find_tape("T-ONE")
            tape.recommended_capacity_bytes = 5
            store.save_tape(tape)
            plan = plan_sources([first, second], capacity="8B", plan_id="capacity-preflight")
            store.save_plan(plan)

            with self.assertRaises(SafetyError):
                apply_plan(store, backend, "capacity-preflight", group_no=1)
            self.assertEqual(len(backend.files), 1, "capacity rejection must happen before append")
            self.assertEqual(store.list_archives(), [])

    def test_catalog_accepts_completed_status_but_rejects_bad_reverse_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="completed-ref")
            store.save_plan(plan)
            apply_plan(store, backend, "completed-ref", group_no=1)
            archive = store.list_archives()[0]

            archive.status = "obsolete"
            store.save_archive(archive)
            self.assertEqual(store.load_plan("completed-ref").execution.status, "complete")

            archive.status = "active"
            store.save_archive(archive)
            tape = store.find_tape("T-ONE")
            tape.archives.clear()
            store.save_tape(tape)
            with self.assertRaises(CatalogError):
                store.load_plan("completed-ref")

    def test_completed_plan_survives_obsolete_status_and_complete_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="obsolete-complete")
            store.save_plan(plan)
            apply_plan(store, backend, plan.plan_id, group_no=1)
            archive = store.list_archives()[0]
            self.assertEqual(mark_obsolete(store, archive.archive_uuid).status, "obsolete")

            # Loading/showing a completed plan is independent of a later
            # retention status transition and does not touch source paths.
            first.unlink()
            loaded = store.load_plan(plan.plan_id)
            self.assertEqual(loaded.execution.status, "complete")

            result = apply_plan(store, backend, plan.plan_id)
            self.assertEqual(result.status, "complete")
            self.assertEqual(len(backend.files), 2)

    def test_nonactive_stale_deterministic_candidates_are_never_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            tape_uuid = store.find_tape("T-ONE").uuid

            for status in ("obsolete", "corrupt"):
                plan = plan_sources([first], capacity="8B", plan_id=f"stale-{status}")
                store.save_plan(plan)
                unit = plan.groups[0].units[0]
                archive_uuid = derive_plan_archive_uuid(plan.plan_id, plan.created_at, 1, unit.unit_id)
                stale = ArchiveRecord(
                    archive_uuid=archive_uuid,
                    name=unit.name,
                    tape_id="T-ONE",
                    tape_uuid=tape_uuid,
                    tape_file_no=2,
                    source_paths=[str(first)],
                    logical_size_bytes=unit.size_bytes,
                    file_count=unit.file_count,
                    tar_stream_sha256="stale-hash",
                    status=status,
                )
                store.save_archive(stale)
                with self.subTest(status=status), self.assertRaises(PlannerError):
                    apply_plan(store, backend, plan.plan_id, group_no=1)
                self.assertEqual(len(backend.files), 1)
                self.assertEqual(store.find_tape("T-ONE").archives, [])

    def test_complete_plan_is_noop_after_source_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="complete-noop")
            store.save_plan(plan)
            apply_plan(store, backend, "complete-noop", group_no=1)
            first.unlink()

            result = apply_plan(store, backend, "complete-noop")
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.skipped_units, [])
            self.assertEqual(len(backend.files), 2)

    def test_cli_json_plan_apply_requires_group_for_unfinished_plan(self) -> None:
        if not HAS_TYPER_RICH:
            self.skipTest("Typer/Rich are optional test dependencies in this checkout")
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self._plan_sources(root, capacity="4B")
            store = CatalogStore(root / "catalog")
            plan = plan_sources([first, second], capacity="4B", plan_id="json-apply")
            store.save_plan(plan)
            result = CliRunner().invoke(
                app,
                ["--catalog", str(store.root), "plan", "apply", "json-apply", "--json"],
            )
            self.assertEqual(result.exit_code, 2, result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("--group", payload["error"])

    def test_cli_apply_can_initialize_blank_tape(self) -> None:
        if not HAS_TYPER_RICH:
            self.skipTest("Typer/Rich are optional test dependencies in this checkout")
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            plan = plan_sources([first], capacity="8B", plan_id="cli-blank")
            store.save_plan(plan)
            backend = MockTapeBackend()
            runner = CliRunner()
            with patch("ltoctl.cli._linux_backend", return_value=backend):
                missing = runner.invoke(
                    app,
                    ["--catalog", str(store.root), "plan", "apply", "cli-blank", "--group", "1", "--json"],
                )
                self.assertEqual(missing.exit_code, 3, missing.stdout)
                self.assertIn("blank", json.loads(missing.stdout)["error"])
                self.assertEqual(backend.files, [])

                json_init = runner.invoke(
                    app,
                    [
                        "--catalog",
                        str(store.root),
                        "plan",
                        "apply",
                        "cli-blank",
                        "--group",
                        "1",
                        "--init-tape",
                        "HOME-001",
                        "--yes",
                        "--json",
                    ],
                )
            self.assertEqual(json_init.exit_code, 0, json_init.stdout)
            payload = json.loads(json_init.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["initialized_tape_id"], "HOME-001")
            self.assertEqual(store.find_tape("HOME-001").tape_id, "HOME-001")

    def test_cli_apply_echoes_initialized_and_start_writing(self) -> None:
        if not HAS_TYPER_RICH:
            self.skipTest("Typer/Rich are optional test dependencies in this checkout")
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            plan = plan_sources([first], capacity="8B", plan_id="cli-echo")
            store.save_plan(plan)
            backend = MockTapeBackend()
            with patch("ltoctl.cli._linux_backend", return_value=backend):
                result = CliRunner().invoke(
                    app,
                    [
                        "--catalog",
                        str(store.root),
                        "plan",
                        "apply",
                        "cli-echo",
                        "--group",
                        "1",
                        "--init-tape",
                        "HOME-001",
                        "--yes",
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.stdout)
            initialized_at = result.stdout.find("initialized HOME-001")
            writing_at = result.stdout.find("start writing")
            self.assertNotEqual(initialized_at, -1, result.stdout)
            self.assertNotEqual(writing_at, -1, result.stdout)
            self.assertLess(initialized_at, writing_at, result.stdout)

    def test_cli_apply_blank_tape_prompt_can_decline(self) -> None:
        if not HAS_TYPER_RICH:
            self.skipTest("Typer/Rich are optional test dependencies in this checkout")
        from ltoctl.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            plan = plan_sources([first], capacity="8B", plan_id="cli-decline")
            store.save_plan(plan)
            backend = MockTapeBackend()
            with patch("ltoctl.cli._linux_backend", return_value=backend):
                declined = CliRunner().invoke(
                    app,
                    ["--catalog", str(store.root), "plan", "apply", "cli-decline", "--group", "1"],
                    input="n\n",
                )
            self.assertEqual(declined.exit_code, 2, declined.stdout)
            self.assertEqual(backend.files, [])
            self.assertEqual(store.load_plan("cli-decline").execution.status, "planned")

    def test_source_drift_is_structured_and_happens_before_binding_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="drift")
            store.save_plan(plan)
            first.write_bytes(b"changed")
            with self.assertRaises(PlanSourceDriftError) as context:
                apply_plan(store, backend, "drift", group_no=1)
            self.assertTrue(context.exception.changes)
            self.assertIn("size changed", context.exception.changes[0].reason)
            self.assertEqual(len(backend.files), 1)
            self.assertEqual(store.load_plan("drift").execution.status, "planned")

    def test_apply_writes_packed_groups_and_ignores_oversized_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small = root / "small"
            huge = root / "huge"
            small.write_bytes(b"1" * 3)
            huge.write_bytes(b"2" * 10)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([small, huge], capacity="8B", plan_id="mixed")
            store.save_plan(plan)
            huge.write_bytes(b"changed-oversized")
            result = apply_plan(store, backend, "mixed", group_no=1)
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.applied_units, [str(small)])
            self.assertEqual(result.oversized_units, [str(huge)])
            saved = store.load_plan("mixed")
            self.assertEqual(saved.execution.status, "complete")
            self.assertEqual(len(saved.oversized_units), 1)
            self.assertEqual(len(backend.files), 2)
            self.assertEqual(len(store.list_archives()), 1)
            no_op = apply_plan(store, backend, "mixed")
            self.assertEqual(no_op.status, "complete")
            self.assertEqual(no_op.oversized_units, [str(huge)])

    def test_apply_refuses_plan_with_no_packed_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            huge = root / "huge"
            huge.write_bytes(b"x" * 10)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            plan = plan_sources([huge], capacity="8B", plan_id="all-oversized")
            store.save_plan(plan)
            with self.assertRaises(PlannerError) as context:
                apply_plan(store, backend, "all-oversized")
            self.assertIn("no packed groups", str(context.exception))
            self.assertEqual(len(backend.files), 1)
            self.assertEqual(store.load_plan("all-oversized").execution.status, "planned")

    def test_apply_initializes_blank_tape_when_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            plan = plan_sources([first], capacity="8B", plan_id="blank-init")
            store.save_plan(plan)
            with self.assertRaises(SafetyError) as missing:
                apply_plan(store, backend, "blank-init", group_no=1)
            self.assertIn("blank", str(missing.exception))
            self.assertEqual(backend.files, [])
            with self.assertRaises(SafetyError):
                apply_plan(
                    store, backend, "blank-init", group_no=1, init_tape_id="HOME-001"
                )
            events: list[tuple[str, dict[str, object]]] = []
            result = apply_plan(
                store,
                backend,
                "blank-init",
                group_no=1,
                init_tape_id="HOME-001",
                confirm_init=True,
                on_progress=lambda event, details, events=events: events.append((event, dict(details))),
            )
            self.assertEqual([event for event, _details in events], ["initialized", "start_writing"])
            self.assertEqual(events[0][1]["tape_id"], "HOME-001")
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.initialized_tape_id, "HOME-001")
            self.assertEqual(result.bound_tape_id, "HOME-001")
            self.assertEqual(store.find_tape("HOME-001").tape_id, "HOME-001")
            self.assertEqual(len(backend.files), 2)
            self.assertEqual(len(store.list_archives()), 1)

    def test_apply_blank_tape_callback_and_bound_group_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            blank = MockTapeBackend()
            plan = plan_sources([first], capacity="8B", plan_id="blank-cb")
            store.save_plan(plan)
            with self.assertRaises(SafetyError) as declined:
                apply_plan(
                    store,
                    blank,
                    "blank-cb",
                    group_no=1,
                    on_blank_tape=lambda group, execution: None,
                )
            self.assertIn("declined", str(declined.exception))
            self.assertEqual(blank.files, [])

            prompted = apply_plan(
                store,
                blank,
                "blank-cb",
                group_no=1,
                on_blank_tape=lambda group, execution: "HOME-002",
            )
            self.assertEqual(prompted.initialized_tape_id, "HOME-002")
            self.assertEqual(prompted.status, "complete")

            bound_store = CatalogStore(root / "bound-catalog")
            cataloged = MockTapeBackend()
            init_tape(bound_store, cataloged, "T-ONE", confirm=True)
            bound_plan = plan_sources([first], capacity="8B", plan_id="bound-blank")
            bound_plan.execution.groups[0].status = "in_progress"
            bound_plan.execution.groups[0].tape_id = "T-ONE"
            bound_plan.execution.groups[0].tape_uuid = bound_store.find_tape("T-ONE").uuid
            bound_plan.execution.status = "in_progress"
            bound_store.save_plan(bound_plan)
            other_blank = MockTapeBackend()
            with self.assertRaises(SafetyError) as bound:
                apply_plan(
                    bound_store,
                    other_blank,
                    "bound-blank",
                    group_no=1,
                    init_tape_id="NEW",
                    confirm_init=True,
                )
            self.assertIn("bound to T-ONE", str(bound.exception))
            self.assertEqual(other_blank.files, [])

            leftover = plan_sources([first], capacity="8B", plan_id="corrupt-skip")
            store.save_plan(leftover)
            corrupt = MockTapeBackend([b"not-a-tar-header"])
            with self.assertRaises(SafetyError):
                apply_plan(
                    store,
                    corrupt,
                    "corrupt-skip",
                    group_no=1,
                    init_tape_id="HOME-003",
                    confirm_init=True,
                )
            self.assertEqual(corrupt.files, [b"not-a-tar-header"])

    def test_remap_requires_confirmation_and_never_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            tape_one = MockTapeBackend()
            tape_two = MockTapeBackend()
            init_tape(store, tape_one, "T-ONE", confirm=True)
            init_tape(store, tape_two, "T-TWO", confirm=True)
            plan = plan_sources([first], capacity="8B", plan_id="remap")
            store.save_plan(plan)
            plan.execution.groups[0].status = "in_progress"
            plan.execution.groups[0].tape_id = "T-ONE"
            plan.execution.groups[0].tape_uuid = store.find_tape("T-ONE").uuid
            plan.execution.status = "in_progress"
            store.save_plan(plan)
            with self.assertRaises(PlannerError):
                apply_plan(store, tape_two, "remap", group_no=1, remap_group=1)
            result = apply_plan(
                store, tape_two, "remap", group_no=1, remap_group=1, confirm_remap=True
            )
            self.assertEqual(result.status, "complete")
            self.assertEqual(store.load_plan("remap").execution.groups[0].tape_id, "T-TWO")
            with self.assertRaises(PlannerError):
                apply_plan(
                    store, tape_one, "remap", group_no=1, remap_group=1, confirm_remap=True
                )

    def test_mark_obsolete_is_idempotent_and_does_not_release_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, _second = self._plan_sources(root, capacity="8B")
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-ONE", confirm=True)
            archive = add_archive(store, backend, [first], name="keep-searchable")
            tape = store.find_tape("T-ONE")
            tape.recommended_capacity_bytes = archive.logical_size_bytes
            store.save_tape(tape)
            marked = mark_obsolete(store, archive.name)
            self.assertEqual(marked.status, "obsolete")
            self.assertEqual(mark_obsolete(store, archive.archive_uuid).status, "obsolete")
            self.assertEqual(store.find_tape("T-ONE").archives, [archive.archive_uuid])
            with self.assertRaises(SafetyError):
                add_archive(store, backend, [first], name="would-exceed-capacity")

    def test_cli_parsers_expose_phase3_commands(self) -> None:
        if not HAS_TYPER_RICH:
            self.skipTest("Typer/Rich are optional test dependencies in this checkout")
        from ltoctl.cli import app

        result = CliRunner().invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.stdout)
        for command in ("drive", "tape", "archive", "plan", "search", "restore", "verify", "catalog"):
            self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
