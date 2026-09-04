"""Non-hardware tests for the Linux ``mt`` backend."""

from __future__ import annotations

from errno import EACCES, EPERM
import io
from unittest.mock import MagicMock, patch
import subprocess
import unittest

from ltoctl.errors import TapeError
from ltoctl.tape.linux_mt import LinuxTapeBackend, parse_mt_status


class LinuxTapeBackendTests(unittest.TestCase):
    def test_status_parses_file_position_flags_and_write_protection(self) -> None:
        output = """SCSI 2 tape drive:
File number=7, block number=0, partition=0.
General status bits on (41010000):
 BOT ONLINE EOD WR_PROT
"""
        status = parse_mt_status(output, device="/dev/nst-test")
        self.assertTrue(status.loaded)
        self.assertFalse(status.writable)
        self.assertTrue(status.at_bot)
        self.assertTrue(status.at_eod)
        self.assertEqual(status.file_no, 7)
        self.assertEqual(status.device, "/dev/nst-test")
        self.assertIsNone(status.error)

    def test_status_reports_no_medium_and_unknown_position_conservatively(self) -> None:
        status = parse_mt_status("mt: /dev/nst-test: No medium present\n", device="/dev/nst-test")
        self.assertFalse(status.loaded)
        self.assertFalse(status.writable)
        self.assertIsNone(status.file_no)
        self.assertIsNotNone(status.error)

        door_open = parse_mt_status(
            "File number=-1\nGeneral status bits on:\n DR_OPEN\n",
            device="/dev/nst-test",
        )
        self.assertFalse(door_open.loaded)
        self.assertIsNotNone(door_open.error)

        unknown = parse_mt_status("File number=-1\nONLINE\n", device="/dev/nst-test")
        self.assertTrue(unknown.loaded)
        self.assertIsNone(unknown.file_no)

    def test_status_invokes_mt_with_explicit_device_and_parses_stderr(self) -> None:
        completed = subprocess.CompletedProcess(
            ["/opt/mt", "-f", "/dev/nst-test", "status"],
            0,
            stdout="File number: 3\nONLINE\n",
            stderr="",
        )
        with patch("ltoctl.tape.linux_mt.subprocess.run", return_value=completed) as run:
            status = LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt").status()
        run.assert_called_once_with(
            ["/opt/mt", "-f", "/dev/nst-test", "status"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertTrue(status.loaded)
        self.assertTrue(status.writable)
        self.assertEqual(status.file_no, 3)

    def test_status_nonzero_command_is_not_loaded(self) -> None:
        completed = subprocess.CompletedProcess(
            ["mt", "-f", "/dev/nst-test", "status"],
            1,
            stdout="",
            stderr="No tape loaded",
        )
        with patch("ltoctl.tape.linux_mt.subprocess.run", return_value=completed):
            status = LinuxTapeBackend("/dev/nst-test", mt_program="mt").status()
        self.assertFalse(status.loaded)
        self.assertFalse(status.writable)
        self.assertIn("No tape loaded", status.error or "")

    def test_motion_commands_are_semantic_and_use_non_rewinding_device(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        backend = LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt")
        with patch("ltoctl.tape.linux_mt.subprocess.run", return_value=completed) as run:
            backend.rewind()
            backend.seek_eod()
            backend.seek_file(2)
            backend.eject()
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/opt/mt", "-f", "/dev/nst-test", "rewind"],
                ["/opt/mt", "-f", "/dev/nst-test", "eom"],
                ["/opt/mt", "-f", "/dev/nst-test", "rewind"],
                ["/opt/mt", "-f", "/dev/nst-test", "fsf", "2"],
                ["/opt/mt", "-f", "/dev/nst-test", "offline"],
            ],
        )

    def test_permission_denied_explains_tape_group_instead_of_sudo(self) -> None:
        denied = subprocess.CompletedProcess(
            ["/opt/mt", "-f", "/dev/nst-test", "status"],
            1,
            stdout="",
            stderr="mt: /dev/nst-test: Permission denied",
        )
        with patch("ltoctl.tape.linux_mt.subprocess.run", return_value=denied):
            status = LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt").status()
        self.assertFalse(status.loaded)
        self.assertIn("permission denied", (status.error or "").lower())
        self.assertIn("usermod -aG", status.error or "")
        self.assertIn("Do not run ltoctl as root", status.error or "")

        with patch(
            "ltoctl.tape.linux_mt.subprocess.run",
            side_effect=PermissionError(EACCES, "Permission denied"),
        ):
            with self.assertRaisesRegex(TapeError, "Do not run ltoctl as root"):
                LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt").rewind()

        open_denied = PermissionError(EPERM, "Permission denied")
        with patch("ltoctl.tape.linux_mt.open", side_effect=open_denied):
            with self.assertRaisesRegex(TapeError, "cannot open tape device for reading"):
                LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt").read_tape_file()

    def test_tape_file_reads_use_record_sized_buffering(self) -> None:
        raw_stream = io.BytesIO()
        with patch("ltoctl.tape.linux_mt.open", return_value=raw_stream) as open_device:
            result = LinuxTapeBackend("/dev/nst-test").read_tape_file()
        self.assertIsInstance(result, io.BufferedReader)
        self.assertTrue(getattr(result, "physical_tape_file", False))
        open_device.assert_called_once_with("/dev/nst-test", "rb", buffering=0)
        result.close()

    def test_linux_writes_are_locked_without_an_explicit_opt_in(self) -> None:
        backend = LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt")
        with patch("ltoctl.tape.linux_mt.subprocess.run") as run:
            with self.assertRaisesRegex(TapeError, "allow_unvalidated_write=True"):
                backend.write_tape_file()
        # The library default stays locked so a bare LinuxTapeBackend cannot
        # accidentally open a device for writing.
        run.assert_not_called()

    def test_explicit_write_opt_in_checks_status_before_opening(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="ONLINE\n", stderr="")
        stream = MagicMock()
        with (
            patch("ltoctl.tape.linux_mt.subprocess.run", return_value=completed),
            patch("ltoctl.tape.linux_mt.open", return_value=stream) as open_device,
        ):
            backend = LinuxTapeBackend(
                "/dev/nst-test",
                mt_program="/opt/mt",
                allow_unvalidated_write=True,
            )
            self.assertIs(backend.write_tape_file(), stream)
        open_device.assert_called_once_with("/dev/nst-test", "wb", buffering=0)

    def test_finish_closes_stream_without_issuing_weof(self) -> None:
        stream = MagicMock()
        stream.fileno.return_value = 42
        backend = LinuxTapeBackend("/dev/nst-test", mt_program="/opt/mt", allow_unvalidated_write=True)
        backend._write_stream = stream
        with patch("ltoctl.tape.linux_mt.os.fsync") as fsync, patch.object(backend, "_mt") as mt:
            backend.finish_tape_file()
        stream.flush.assert_called_once_with()
        fsync.assert_called_once_with(42)
        stream.close.assert_called_once_with()
        mt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
