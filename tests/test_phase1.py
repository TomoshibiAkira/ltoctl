from __future__ import annotations

import json
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ltoctl.archive.scanner import scan_sources
from ltoctl.catalog.index import INDEX_COLUMNS
from ltoctl.catalog.index import rebuild_index, search_index
from ltoctl.catalog.models import ArchiveRecord, OperationRecord, TapeRecord
from ltoctl.catalog.store import CatalogStore
from ltoctl.catalog.validation import validate_catalog
from ltoctl.config import load_config
from ltoctl.errors import CatalogError, CatalogValidationError, PlannerError, ScanError, TapeError
from ltoctl.planner.packing import pack_units_ffd
from ltoctl.planner.scanner import plan_sources, rescan_plan
from ltoctl.tape.linux_mt import LinuxTapeBackend
from ltoctl.tape.mock import MockTapeBackend
from ltoctl.utils.atomic import atomic_write_stream
from ltoctl.utils.units import parse_bytes

try:
    import rich  # noqa: F401
    import typer  # noqa: F401

    HAS_TYPER_RICH = True
except ImportError:
    HAS_TYPER_RICH = False


class Phase1Tests(unittest.TestCase):
    def test_scanner_is_lexical_and_streams_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "A"
            root.mkdir()
            (root / "z").mkdir()
            (root / "z" / "y").write_text("y", encoding="utf-8")
            (root / "x").write_text("xx", encoding="utf-8")
            (Path(temporary) / "B").write_text("b", encoding="utf-8")
            manifest = Path(temporary) / "manifest.jsonl"
            result = scan_sources([root, Path(temporary) / "B"], manifest_path=manifest)
            paths = [json.loads(line)["path"] for line in manifest.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(paths, ["A", "A/x", "A/z", "A/z/y", "B"])
            self.assertEqual(result.logical_size_bytes, 4)
            self.assertEqual(result.file_count, 3)

    def test_planner_is_deterministic_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"1" * 7)
            second.write_bytes(b"2" * 3)
            plan = plan_sources([first, second], capacity="8B", plan_id="demo")
            self.assertEqual(plan.tape_count, 2)
            self.assertEqual(plan.groups[0].units[0].name, "first")
            first.write_bytes(b"1" * 9)
            changes = rescan_plan(plan)
            self.assertEqual(len(changes), 1)
            self.assertIn("size changed", changes[0].reason)

    def test_planner_reports_zero_packed_tapes_when_every_unit_is_oversized(self) -> None:
        from ltoctl.cli import _human_plan

        with tempfile.TemporaryDirectory() as temporary:
            huge = Path(temporary) / "dataset"
            huge.write_bytes(b"x" * 10)
            plan = plan_sources([huge], capacity="8B", plan_id="oversized")
            self.assertEqual(plan.tape_count, 0)
            self.assertEqual(len(plan.groups), 0)
            self.assertEqual(len(plan.oversized_units), 1)
            text = _human_plan(plan)
            self.assertIn("Estimated tapes: 0", text)
            self.assertIn("Oversized units not packed: 1", text)
            self.assertIn("--unit-depth", text)

    def test_rescan_packed_only_ignores_oversized_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small = root / "small"
            huge = root / "huge"
            small.write_bytes(b"1" * 3)
            huge.write_bytes(b"2" * 10)
            plan = plan_sources([small, huge], capacity="8B", plan_id="mixed")
            self.assertEqual(plan.tape_count, 1)
            self.assertEqual(len(plan.oversized_units), 1)
            huge.write_bytes(b"2" * 12)
            self.assertEqual(rescan_plan(plan, packed_only=True), [])
            changes = rescan_plan(plan)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].path, str(huge))

    def test_catalog_roundtrip_index_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            tape = TapeRecord.new("LTO6-001")
            store.save_tape(tape)
            archive = ArchiveRecord(
                archive_uuid="archive-1",
                name="Photos",
                tape_id=tape.tape_id,
                tape_uuid=tape.uuid,
                tape_file_no=1,
                source_paths=["/data/photos"],
                logical_size_bytes=5,
                file_count=2,
            )
            store.save_archive(archive)
            store.save_manifest(
                archive.archive_uuid,
                [
                    {"path": "Photos/a", "size": 3, "mtime_ns": 1, "type": "file"},
                    {"path": "Photos/b", "size": 2, "mtime_ns": 2, "type": "file"},
                ],
            )
            # Keep the reverse tape reference in canonical state, as an
            # archive append would do.
            tape.archives.append(archive.archive_uuid)
            store.save_tape(tape)
            report = validate_catalog(store)
            self.assertTrue(report.ok, report.errors)
            index_path = rebuild_index(store)
            results = list(search_index(index_path, "A"))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].archive_name, "Photos")

    def test_mock_tape_never_overwrites_and_failed_ops_block(self) -> None:
        tape = MockTapeBackend([b"header"])
        tape.seek_eod()
        self.assertEqual(tape.append_file(b"archive"), 1)
        self.assertEqual(tape.files, [b"header", b"archive"])
        tape.seek_file(0)
        with self.assertRaises(TapeError):
            tape.write_tape_file()

        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            operation = OperationRecord.new("LTO6-001", "tape-uuid")
            operation.state = "failed"
            store.save_operation(operation)
            self.assertEqual(len(store.unresolved_operations("tape-uuid")), 1)
            operation.state = "catalog_committed"
            store.save_operation(operation)
            self.assertEqual(store.unresolved_operations("tape-uuid"), [])

    def test_models_reject_unsafe_file_numbers_and_inconsistent_groups(self) -> None:
        with self.assertRaises(CatalogValidationError):
            ArchiveRecord("archive-0", "bad", "T", "uuid", 0)
        from ltoctl.catalog.models import PlanGroup, PlanUnit

        one = PlanUnit("/one", "one", 5, 1, 1)
        with self.assertRaises(CatalogValidationError):
            PlanGroup(group_no=1, units=[one], used_bytes=4, capacity_bytes=10)
        group = PlanGroup(group_no=1, capacity_bytes=10)
        group.add_unit(one)
        self.assertEqual(group.used_bytes, 5)
        with self.assertRaises(CatalogValidationError):
            group.add_unit(PlanUnit("/two", "two", 6, 1, 1))
        group.units.append(PlanUnit("/two", "two", 1, 1, 1))
        with self.assertRaises(CatalogValidationError):
            group.validate_consistency()

    def test_plan_partition_invariant_rejects_corrupt_roundtrips(self) -> None:
        from copy import deepcopy
        from ltoctl.catalog.models import PlanGroup, PlanRecord, PlanUnit

        one = PlanUnit("/one", "one", 4, 1, 1)
        two = PlanUnit("/two", "two", 6, 1, 1)
        valid = PlanRecord(
            plan_id="partition",
            created_at="now",
            media_type="LTO-6",
            nominal_capacity_bytes=10,
            recommended_capacity_bytes=10,
            usable_capacity_bytes=10,
            packing_algorithm="first_fit_decreasing",
            source_paths=["/one", "/two"],
            units=[one, two],
            groups=[PlanGroup(1, [one, two], 10, 10)],
        )
        self.assertEqual(PlanRecord.from_dict(valid.to_dict()).tape_count, 1)

        corruptions = {}
        duplicate_top = deepcopy(valid.to_dict())
        duplicate_top["units"].append(deepcopy(duplicate_top["units"][0]))
        corruptions["duplicate top-level unit"] = duplicate_top

        absent_group_unit = deepcopy(valid.to_dict())
        absent_group_unit["groups"][0]["units"][0]["path"] = "/not-in-top-level"
        corruptions["group unit absent"] = absent_group_unit

        duplicate_group = deepcopy(valid.to_dict())
        second_group = deepcopy(duplicate_group["groups"][0])
        second_group["group_no"] = 2
        duplicate_group["groups"].append(second_group)
        corruptions["cross-group duplicate"] = duplicate_group

        nonconsecutive = deepcopy(valid.to_dict())
        nonconsecutive["groups"][0]["group_no"] = 2
        corruptions["non-consecutive group number"] = nonconsecutive

        wrong_group_capacity = deepcopy(valid.to_dict())
        wrong_group_capacity["groups"][0]["capacity_bytes"] = 9
        corruptions["group capacity mismatch"] = wrong_group_capacity

        missing_unit = deepcopy(valid.to_dict())
        missing_unit["groups"][0]["units"] = [deepcopy(missing_unit["groups"][0]["units"][0])]
        missing_unit["groups"][0]["used_bytes"] = 4
        corruptions["missing non-oversized unit"] = missing_unit

        bad_oversized = deepcopy(valid.to_dict())
        bad_oversized["units"][0]["oversized"] = True
        bad_oversized["oversized_units"] = [deepcopy(bad_oversized["units"][0])]
        corruptions["oversized not over capacity"] = bad_oversized

        oversized_group_disagreement = deepcopy(valid.to_dict())
        oversized_group_disagreement["groups"][0]["units"][0]["oversized"] = True
        corruptions["group oversized disagreement"] = oversized_group_disagreement

        for label, data in corruptions.items():
            with self.subTest(label=label), self.assertRaises(CatalogValidationError):
                PlanRecord.from_dict(data)

    def test_scanner_rejects_missing_duplicate_and_reserved_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").mkdir()
            (root / "other").mkdir()
            (root / "__LTOCTL__").mkdir()
            with self.assertRaises(ScanError):
                scan_sources([root / "missing"])
            with self.assertRaises(ScanError):
                scan_sources([root / "one", root / "other" / ".." / "one"])
            with self.assertRaises(ScanError):
                scan_sources([root / "__LTOCTL__"])

    def test_atomic_stream_preserves_previous_file_on_generator_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "data"
            destination.write_text("old", encoding="utf-8")

            def chunks():
                yield "new"
                raise RuntimeError("simulated writer failure")

            with self.assertRaises(RuntimeError):
                atomic_write_stream(destination, chunks())
            self.assertEqual(destination.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(temporary).glob(".*.tmp")), [])

    def test_index_search_modes_and_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            tape = TapeRecord.new("T")
            store.save_tape(tape)
            archive = ArchiveRecord("archive-1", "Photos", "T", tape.uuid, 1)
            store.save_archive(archive)
            store.save_manifest(
                "archive-1",
                [
                    {"path": "Photos/IMG_1.CR2", "size": 1, "mtime_ns": 1, "type": "file"},
                    {"path": "Photos/readme.txt", "size": 2, "mtime_ns": 2, "type": "file"},
                ],
            )
            index = rebuild_index(store)
            self.assertEqual(tuple(index.read_text(encoding="utf-8").splitlines()[0].split("\t")), INDEX_COLUMNS)
            self.assertEqual(len(list(search_index(index, "Photos/IMG_1.CR2", exact=True))), 1)
            self.assertEqual(len(list(search_index(index, r"IMG_[0-9]+", regex=True))), 1)
            self.assertEqual(len(list(search_index(index, "read", tape="other"))), 0)
            with self.assertRaises(CatalogError):
                list(search_index(Path(temporary) / "missing.tsv", "x"))

    def test_cli_search_json_is_streamed_as_valid_array(self) -> None:
        from ltoctl.cli import _stream_search

        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog")
            tape = TapeRecord.new("T")
            store.save_tape(tape)
            archive = ArchiveRecord("archive-1", "Photos", "T", tape.uuid, 1)
            store.save_archive(archive)
            store.save_manifest(
                "archive-1",
                [{"path": f"Photos/{index}.dat", "size": index, "mtime_ns": index, "type": "file"} for index in range(3)],
            )
            rebuild_index(store)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                _stream_search(search_index(store.root / "index" / "files.tsv", ".dat"), json_mode=True)
            decoded = json.loads(output.getvalue())
            self.assertEqual(len(decoded), 3)

    def test_linux_writes_are_explicitly_locked_and_no_weof_strategy(self) -> None:
        backend = LinuxTapeBackend()
        with self.assertRaises(TapeError):
            backend.write_tape_file()
        with self.assertRaises(ValueError):
            LinuxTapeBackend(filemark_strategy="weof")
        self.assertEqual(LinuxTapeBackend(allow_unvalidated_write=True).filemark_strategy, "close")

    def test_production_cli_enables_qualified_linux_writes(self) -> None:
        from ltoctl.cli import _linux_backend

        backend = _linux_backend("/dev/nst-test")
        self.assertTrue(backend.allow_unvalidated_write)
        self.assertEqual(backend.filemark_strategy, "close")

    def test_config_environment_overrides_file_and_media_default_reaches_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_file = Path(temporary) / "config.toml"
            config_file.write_text(
                'media = "lto5"\nlog_path = "/from/file.log"\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"LTOCTL_LOG": "/from/env.log", "LTOCTL_MEDIA": "lto7"}, clear=False):
                config = load_config(config_file=config_file)
            self.assertEqual(config.media, "lto7")
            self.assertEqual(config.log_path, Path("/from/env.log"))

    def test_cli_plan_media_uses_config_when_option_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.write_bytes(b"payload")
            xdg = Path(temporary) / "xdg"
            config_dir = xdg / "ltoctl"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text('media = "lto5"\n', encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False):
                config = load_config(catalog_root=Path(temporary) / "catalog")
                plan = plan_sources([source], media=config.media)
            self.assertEqual(plan.media_type, "LTO-5")

    @unittest.skipUnless(HAS_TYPER_RICH, "Typer/Rich are optional test dependencies in this checkout")
    def test_typer_rich_app_help_smoke(self) -> None:
        from typer.testing import CliRunner
        from ltoctl.cli import app

        result = CliRunner().invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("ltoctl", result.stdout)

    def test_capacity_parser(self) -> None:
        self.assertEqual(parse_bytes("2.30TB"), 2_300_000_000_000)
        self.assertEqual(parse_bytes("2KiB"), 2048)


if __name__ == "__main__":
    unittest.main()
