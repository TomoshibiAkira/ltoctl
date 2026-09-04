from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from ltoctl.archive.reader import restore_archive
from ltoctl.archive.reconcile import reconcile_tape
from ltoctl.archive.verify import verify_archive
from ltoctl.archive.writer import add_archive
from ltoctl.catalog.models import ArchiveRecord, ManifestEntry, OperationRecord, TapeRecord
from ltoctl.catalog.store import CatalogStore
from ltoctl.errors import ArchiveError, RestoreError, SafetyError, ScanError, TapeError, VerificationError
from ltoctl.tape.mock import MockTapeBackend
from ltoctl.tape.service import init_tape
from ltoctl.tape.header import TAPE_HEADER_MEMBER, build_tape_header, parse_tape_header


class FailingJournalStore(CatalogStore):
    """Inject one durable journal failure after the physical write."""

    def __init__(self, root: Path):
        self.fail_tape_write_finished = False
        super().__init__(root)

    def save_operation(self, record):
        if self.fail_tape_write_finished and record.state == "tape_write_finished":
            raise OSError("simulated operation journal failure")
        return super().save_operation(record)


class FailingArchiveCommitStore(CatalogStore):
    """Inject a canonical archive JSON failure after tape completion."""

    def __init__(self, root: Path):
        self.fail_archive_commit = False
        super().__init__(root)

    def save_archive(self, record):
        if self.fail_archive_commit:
            raise OSError("simulated archive catalog failure")
        return super().save_archive(record)


class TapeWorkflowTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "photos"
        (source / "nested").mkdir(parents=True)
        (source / "a.txt").write_text("alpha", encoding="utf-8")
        (source / "nested" / "b.bin").write_bytes(b"bravo" * 3)
        return source

    def test_init_add_verify_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init = init_tape(store, backend, "T-001", media="lto6", confirm=True)
            self.assertEqual(init.tape.tape_id, "T-001")
            self.assertEqual(backend.file_count, 1)
            source = self._source(root)

            archive = add_archive(store, backend, [source], name="photos")
            self.assertEqual(archive.tape_file_no, 1)
            self.assertIsNotNone(archive.tar_stream_sha256)
            self.assertEqual(backend.file_count, 2)
            self.assertEqual(store.find_tape("T-001").archives, [archive.archive_uuid])
            self.assertTrue(verify_archive(store, backend, archive).ok)

            output = root / "restore"
            restored = restore_archive(store, backend, archive.archive_uuid, output)
            self.assertEqual(restored.extracted_count, 4)
            self.assertIsNone(restored.extracted)
            self.assertEqual((output / "photos" / "a.txt").read_text(encoding="utf-8"), "alpha")
            self.assertEqual((output / "photos" / "nested" / "b.bin").read_bytes(), b"bravo" * 3)

            with self.assertRaises(RestoreError):
                restore_archive(store, backend, archive.name, output)
            selected_output = root / "selected"
            selected = restore_archive(
                store,
                backend,
                archive.name,
                selected_output,
                selected_files=["photos/nested"],
            )
            self.assertEqual(selected.extracted, ("photos/nested",))
            self.assertFalse((selected_output / "photos" / "a.txt").exists())

    def test_hardlinks_are_written_as_regular_payload_and_trailing_bytes_fail_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = root / "links"
            source.mkdir()
            (source / "first").write_bytes(b"same")
            (source / "second").hardlink_to(source / "first")
            (source / "link").symlink_to("first")
            archive = add_archive(store, backend, [source], name="links")
            output = root / "restore"
            restore_archive(store, backend, archive.name, output)
            self.assertEqual((output / "links" / "first").read_bytes(), b"same")
            self.assertEqual((output / "links" / "second").read_bytes(), b"same")
            self.assertTrue((output / "links" / "link").is_symlink())
            self.assertEqual(os.readlink(output / "links" / "link"), "first")
            self.assertTrue(verify_archive(store, backend, archive).ok)
            backend.files[1] += b"trailing-garbage"
            failed = verify_archive(store, backend, archive)
            self.assertFalse(failed.ok)
            self.assertIn("trailing", failed.error or "")

    def test_init_requires_confirmation_and_append_rejects_existing_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            with self.assertRaises(SafetyError):
                init_tape(store, backend, "T-001", confirm=False)
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            archive = add_archive(store, backend, [source], name="one", archive_uuid="fixed-uuid")
            with self.assertRaises(ArchiveError):
                add_archive(store, backend, [source], name="two", archive_uuid=archive.archive_uuid)
            self.assertEqual(backend.file_count, 2)

    def test_write_protection_source_mutation_and_capacity_accounting_are_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend(writable=False)
            with self.assertRaises(SafetyError):
                init_tape(store, backend, "T-001", confirm=True)
            backend.writable = True
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            backend.writable = False
            with self.assertRaises(SafetyError):
                add_archive(store, backend, [source], name="protected")
            backend.writable = True

            def mutate_source() -> None:
                (source / "changed.txt").write_text("changed", encoding="utf-8")

            with self.assertRaises(ArchiveError):
                add_archive(store, backend, [source], name="mutated", before_write=mutate_source)
            self.assertFalse(any(record.archive_name == "mutated" for record in store.list_operations()))
            self.assertEqual(backend.file_count, 1)
            self.assertTrue(reconcile_tape(store, backend).ok)
            self.assertEqual(store.find_tape("T-001").status, "active")

            # A catalog status change never frees already-written physical
            # space; accounting must include obsolete records as well.  Use a
            # clean tape because the deliberate pre-write failure above is
            # itself an unresolved append blocker.
            capacity_store = CatalogStore(root / "capacity-catalog")
            capacity_backend = MockTapeBackend()
            init_tape(capacity_store, capacity_backend, "T-CAP", confirm=True)
            archive = add_archive(capacity_store, capacity_backend, [source], name="first")
            tape = capacity_store.find_tape("T-CAP")
            archive.status = "obsolete"
            capacity_store.save_archive(archive)
            tape.recommended_capacity_bytes = archive.logical_size_bytes
            capacity_store.save_tape(tape)
            with self.assertRaises(SafetyError):
                add_archive(capacity_store, capacity_backend, [source], name="over-budget")

    def test_prepared_operation_is_aborted_only_when_eod_proves_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init = init_tape(store, backend, "T-001", confirm=True)
            prepared = OperationRecord.new(
                "T-001",
                init.tape.uuid,
                archive_uuid="prepared-only",
                archive_name="prepared-only",
                expected_tape_file_no=1,
            )
            store.save_operation(prepared)
            result = reconcile_tape(store, backend, operation_uuid=prepared.operation_uuid)
            self.assertTrue(result.ok, result.messages)
            self.assertEqual(store.load_operation(prepared.operation_uuid).state, "aborted")
            self.assertEqual(store.find_tape("T-001").status, "active")
            repeat = reconcile_tape(store, backend, operation_uuid=prepared.operation_uuid)
            self.assertTrue(repeat.ok, repeat.messages)

            conservative_store = CatalogStore(root / "conservative-catalog")
            conservative_backend = MockTapeBackend()
            conservative_init = init_tape(conservative_store, conservative_backend, "T-002", confirm=True)
            writing = OperationRecord.new(
                "T-002",
                conservative_init.tape.uuid,
                archive_uuid="writing-same-eod",
                archive_name="writing-same-eod",
                expected_tape_file_no=1,
            )
            writing.state = "writing"
            conservative_store.save_operation(writing)
            result = reconcile_tape(conservative_store, conservative_backend, operation_uuid=writing.operation_uuid)
            self.assertFalse(result.ok)
            self.assertTrue(result.needs_recovery)
            self.assertEqual(conservative_store.load_operation(writing.operation_uuid).state, "writing")
            self.assertEqual(conservative_store.find_tape("T-002").status, "needs_recovery")

    def test_unknown_corrupt_and_uuid_mismatch_headers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")

            unknown = TapeRecord.new("UNKNOWN")
            backend = MockTapeBackend([build_tape_header(unknown)])
            with self.assertRaises(SafetyError):
                from ltoctl.tape.safety import TapeSafetyService

                TapeSafetyService(store).identify_loaded(backend)

            catalog_tape = TapeRecord.new("KNOWN")
            store.save_tape(catalog_tape)
            different_uuid = TapeRecord.new("KNOWN")
            backend = MockTapeBackend([build_tape_header(different_uuid)])
            with self.assertRaises(SafetyError):
                from ltoctl.tape.safety import TapeSafetyService

                TapeSafetyService(store).identify_loaded(backend)

            corrupt = MockTapeBackend([b"not-a-tar-header"])
            with self.assertRaises(SafetyError):
                from ltoctl.tape.safety import TapeSafetyService

                TapeSafetyService(store).identify_loaded(corrupt)

    def test_eod_mismatch_and_missing_physical_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            archive = add_archive(store, backend, [source], name="one")
            backend.files.pop()
            with self.assertRaises(SafetyError):
                from ltoctl.tape.safety import TapeSafetyService

                TapeSafetyService(store).assert_append_ready(backend)
            self.assertEqual(archive.tape_file_no, 1)

    def test_catalog_commit_failure_leaves_tape_write_finished_then_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FailingArchiveCommitStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            store.fail_archive_commit = True
            with self.assertRaises(OSError):
                add_archive(store, backend, [source], name="commit-failure")
            operation = next(record for record in store.list_operations() if record.archive_name == "commit-failure")
            self.assertEqual(operation.state, "tape_write_finished")
            self.assertEqual(backend.file_count, 2)
            store.fail_archive_commit = False
            result = reconcile_tape(store, backend, operation_uuid=operation.operation_uuid)
            self.assertTrue(result.ok, result.messages)
            self.assertEqual(store.load_operation(operation.operation_uuid).state, "catalog_committed")

    def test_hash_and_embedded_archive_uuid_mismatches_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            archive = add_archive(store, backend, [source], name="integrity")
            original = backend.files[1]
            backend.files[1] = original.replace(b"alpha", b"ALPHA", 1)
            failed = verify_archive(store, backend, archive)
            self.assertFalse(failed.ok)
            self.assertIn("hash", failed.error or "")
            with self.assertRaises(RestoreError):
                restore_archive(store, backend, archive.name, root / "hash-failure")
            backend.files[1] = original.replace(archive.archive_uuid.encode(), b"0" * len(archive.archive_uuid), 1)
            failed_uuid = verify_archive(store, backend, archive)
            self.assertFalse(failed_uuid.ok)
            self.assertIn("UUID", failed_uuid.error or "")

    def test_restore_rejects_path_traversal_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            tape = TapeRecord.new("T-001")
            store.save_tape(tape)
            record = ArchiveRecord(
                archive_uuid="evil-archive",
                name="evil",
                tape_id=tape.tape_id,
                tape_uuid=tape.uuid,
                tape_file_no=1,
                source_paths=["/untrusted"],
                logical_size_bytes=1,
                file_count=1,
            )
            descriptor = dict(record.to_dict())
            descriptor.pop("tar_stream_sha256", None)
            manifest = ManifestEntry("../../escape.txt", 1, 0, "file")
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for name, payload in (
                    ("__LTOCTL__/archive.json", json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()),
                    ("__LTOCTL__/manifest.jsonl", (json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mtime = 0
                    tar.addfile(info, io.BytesIO(payload))
                info = tarfile.TarInfo("../../escape.txt")
                info.size = 1
                info.mtime = 0
                tar.addfile(info, io.BytesIO(b"x"))
            payload = output.getvalue()
            backend.files = [backend.files[0] if backend.files else build_tape_header(tape), payload]
            record.tar_stream_sha256 = hashlib.sha256(payload).hexdigest()
            store.save_archive(record)
            store.save_manifest(record.archive_uuid, [manifest])
            tape.archives.append(record.archive_uuid)
            store.save_tape(tape)
            with self.assertRaises(RestoreError):
                restore_archive(store, backend, record.archive_uuid, root / "safe-output")
            self.assertFalse((root / "escape.txt").exists())

    def test_write_failure_is_journaled_and_blocks_next_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            backend.fail_on_write = True
            with self.assertRaises(TapeError):
                add_archive(store, backend, [source], name="failed")
            operations = store.list_operations()
            self.assertEqual(len(operations), 2)
            failed_operation = next(record for record in operations if record.archive_name == "failed")
            self.assertEqual(failed_operation.state, "failed")
            backend.fail_on_write = False
            with self.assertRaises(SafetyError):
                add_archive(store, backend, [source], name="blocked")

    def test_reconcile_complete_physical_file_after_journal_failure_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FailingJournalStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            store.fail_tape_write_finished = True
            with self.assertRaises(OSError):
                add_archive(store, backend, [source], name="recovered")
            self.assertEqual(backend.file_count, 2)
            operation = next(record for record in store.list_operations() if record.archive_name == "recovered")
            self.assertEqual(operation.state, "writing")
            store.fail_tape_write_finished = False

            result = reconcile_tape(store, backend)
            self.assertTrue(result.ok, result.messages)
            self.assertEqual(result.reconciled_operations, [operation.operation_uuid])
            self.assertEqual(store.list_archives()[0].name, "recovered")
            repeat = reconcile_tape(store, backend)
            self.assertTrue(repeat.ok)
            self.assertEqual(repeat.reconciled_operations, [])

    def test_reconcile_incomplete_file_marks_tape_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init = init_tape(store, backend, "T-001", confirm=True)
            operation = OperationRecord.new(
                "T-001",
                init.tape.uuid,
                archive_uuid="missing-archive",
                archive_name="bad",
                expected_tape_file_no=1,
            )
            operation.state = "writing"
            store.save_operation(operation)
            backend.seek_eod()
            backend.append_file(b"not a tar stream")
            result = reconcile_tape(store, backend)
            self.assertFalse(result.ok)
            self.assertTrue(result.needs_recovery)
            self.assertEqual(store.find_tape("T-001").status, "needs_recovery")
            self.assertEqual(store.load_operation(operation.operation_uuid).state, "writing")

    def test_extra_physical_file_blocks_reconcile_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            backend.seek_eod()
            backend.append_file(b"un-cataloged")
            with self.assertRaises(SafetyError):
                from ltoctl.tape.safety import TapeSafetyService

                TapeSafetyService(store).assert_append_ready(backend)
            with self.assertRaises(VerificationError):
                from ltoctl.archive.verify import verify_tape

                verify_tape(store, backend, "T-001")

    def test_restore_preflight_does_not_delete_existing_data_on_late_conflict(self) -> None:
        """A later type conflict must be found before any overwrite commit."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            archive = add_archive(store, backend, [self._source(root)], name="overwrite-preflight")

            output = root / "existing"
            (output / "photos").mkdir(parents=True)
            (output / "photos" / "a.txt").write_text("old-a", encoding="utf-8")
            # Manifest order reaches this directory after photos/a.txt.  It
            # is intentionally the wrong type so preflight must fail late.
            (output / "photos" / "nested").write_text("old-nested", encoding="utf-8")

            with self.assertRaises(RestoreError):
                restore_archive(store, backend, archive.name, output, overwrite=True)
            self.assertEqual((output / "photos" / "a.txt").read_text(encoding="utf-8"), "old-a")
            self.assertTrue((output / "photos" / "nested").is_file())
            self.assertEqual((output / "photos" / "nested").read_text(encoding="utf-8"), "old-nested")

    def test_directory_modes_are_applied_after_restore_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = self._source(root)
            os.chmod(source / "nested", 0o555)
            os.chmod(source, 0o555)
            try:
                archive = add_archive(store, backend, [source], name="readonly-dirs")
                output = root / "readonly-restore"
                restore_archive(store, backend, archive.name, output)
                self.assertEqual(os.stat(output / "photos").st_mode & 0o7777, 0o555)
                self.assertEqual(os.stat(output / "photos" / "nested").st_mode & 0o7777, 0o555)
                self.assertEqual((output / "photos" / "nested" / "b.bin").read_bytes(), b"bravo" * 3)
            finally:
                # Keep TemporaryDirectory cleanup writable even when an
                # assertion fails after the archive was successfully made.
                os.chmod(source / "nested", 0o755)
                os.chmod(source, 0o755)

    def test_header_rejects_nonzero_trailing_bytes_and_extra_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tape = TapeRecord.new("T-001")
            with self.assertRaises(TapeError):
                parse_tape_header(build_tape_header(tape) + b"nonzero-tail")

            payload = json.dumps(tape.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                header = tarfile.TarInfo(TAPE_HEADER_MEMBER)
                header.size = len(payload)
                header.mtime = 0
                archive.addfile(header, io.BytesIO(payload))
                extra = tarfile.TarInfo("unexpected")
                extra.size = 1
                extra.mtime = 0
                archive.addfile(extra, io.BytesIO(b"x"))
            with self.assertRaises(TapeError):
                parse_tape_header(output.getvalue())

    def test_scanner_rejects_fifo_and_archive_root_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ScanError):
                add_archive(store, backend, [fifo], name="fifo")

            source = root / "links"
            source.mkdir()
            (source / "escape").symlink_to("../../outside")
            with self.assertRaises(ScanError):
                add_archive(store, backend, [source], name="escaping-link")
            self.assertEqual(backend.file_count, 1)
            self.assertEqual(store.list_operations()[0].state, "catalog_committed")

    def test_whole_restore_result_does_not_materialize_all_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog")
            backend = MockTapeBackend()
            init_tape(store, backend, "T-001", confirm=True)
            source = root / "bulk"
            source.mkdir()
            for index in range(128):
                (source / f"file-{index:03d}.txt").write_text(str(index), encoding="utf-8")
            archive = add_archive(store, backend, [source], name="bulk")
            result = restore_archive(store, backend, archive.name, root / "bulk-restore")
            self.assertEqual(result.extracted_count, 129)
            self.assertIsNone(result.extracted)

            selected = restore_archive(
                store,
                backend,
                archive.name,
                root / "bulk-selected-restore",
                selected_files=["bulk"],
            )
            self.assertEqual(selected.extracted_count, 129)
            self.assertEqual(selected.extracted, ("bulk",))


if __name__ == "__main__":
    unittest.main()
