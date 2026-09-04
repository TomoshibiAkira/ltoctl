"""Destructive, opt-in qualification for a real Linux tape drive.

This module is intentionally excluded by the default pytest configuration.
It must not acquire a device, load a catalog, or perform a write unless the
marker is selected and every explicit environment guard below is present.
The target cartridge must be disposable: initialization writes physical file
0 and the two qualification archives consume additional tape files.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

import pytest

from ltoctl.archive.reader import restore_archive
from ltoctl.archive.verify import verify_tape
from ltoctl.archive.writer import add_archive
from ltoctl.catalog.store import CatalogStore
from ltoctl.tape.linux_mt import LinuxTapeBackend
from ltoctl.tape.safety import TapeSafetyService
from ltoctl.tape.service import init_tape


ENABLE_ENV = "LTOCTL_TAPE_HARDWARE_ENABLE"
ACK_ENV = "LTOCTL_TAPE_HARDWARE_ACK"
DEVICE_ENV = "LTOCTL_TAPE_HARDWARE_DEVICE"
TAPE_ID_ENV = "LTOCTL_TAPE_HARDWARE_TAPE_ID"
MT_ENV = "LTOCTL_TAPE_HARDWARE_MT"
DESTRUCTIVE_ACK = "I_UNDERSTAND_THIS_IS_A_DISPOSABLE_TAPE"


@dataclass(frozen=True)
class _HardwareConfiguration:
    device: str
    tape_id: str
    mt_program: str


def _guarded_configuration() -> _HardwareConfiguration:
    """Return explicit hardware settings, or skip before touching a drive."""

    if os.environ.get(ENABLE_ENV) != "1":
        pytest.skip(f"set {ENABLE_ENV}=1 to opt into destructive tape qualification")
    if os.environ.get(ACK_ENV) != DESTRUCTIVE_ACK:
        pytest.skip(
            f"set {ACK_ENV}={DESTRUCTIVE_ACK} to acknowledge disposable-media destruction"
        )

    # No fallback to Config/LTOCTL_DEVICE/LinuxTapeBackend's /dev/nst0 is
    # allowed here.  A missing or blank device guard always skips before a
    # Linux backend is constructed.
    device = os.environ.get(DEVICE_ENV)
    if not device or not device.strip():
        pytest.skip(f"set {DEVICE_ENV} to the explicit non-rewinding tape device")
    if device != device.strip():
        pytest.skip(f"{DEVICE_ENV} must not contain leading/trailing whitespace")

    tape_id = os.environ.get(TAPE_ID_ENV)
    if not tape_id or not tape_id.strip():
        pytest.skip(f"set {TAPE_ID_ENV} to the expected disposable tape ID")
    if tape_id != tape_id.strip():
        pytest.skip(f"{TAPE_ID_ENV} must not contain leading/trailing whitespace")

    configured_mt = os.environ.get(MT_ENV)
    if configured_mt:
        mt_program = shutil.which(configured_mt) or configured_mt
        if not Path(mt_program).is_file():
            pytest.skip(f"configured mt utility does not exist: {configured_mt}")
    else:
        mt_program = shutil.which("mt")
        if mt_program is None:
            pytest.skip("the optional mt utility is not installed")

    return _HardwareConfiguration(device=device, tape_id=tape_id, mt_program=mt_program)


@pytest.mark.tape_hardware
def test_disposable_linux_tape_qualification(tmp_path: Path) -> None:
    """Qualify filemarks, numbering, cross-process reads, restore, and eject."""

    configuration = _guarded_configuration()
    # Direct LinuxTapeBackend construction stays write-locked by default.
    # The production CLI opts into the qualified close-only path separately.
    backend = LinuxTapeBackend(
        configuration.device,
        mt_program=configuration.mt_program,
        allow_unvalidated_write=True,
    )
    active_backend = backend
    ejected = False
    media_loaded = False
    qualification_succeeded = False

    try:
        initial_status = backend.status()
        media_loaded = initial_status.loaded
        assert initial_status.loaded, initial_status.error or "no tape is loaded"
        assert initial_status.writable, "qualification tape is write protected"

        # Readiness for initialization is a real blank-tape check.  The
        # service repeats this check before writing file 0.
        backend.seek_eod()
        assert backend.current_file_no() == 0, "qualification requires a blank tape at physical EOD 0"
        backend.rewind()
        assert backend.current_file_no() == 0

        initialized = init_tape(
            CatalogStore(tmp_path / "catalog"),
            backend,
            configuration.tape_id,
            media="lto6",
            confirm=True,
        )
        store = CatalogStore(tmp_path / "catalog")
        assert initialized.tape.tape_id == configuration.tape_id
        assert backend.current_file_no() == 1
        ready = TapeSafetyService(store).assert_append_ready(backend)
        assert ready.next_file_no == 1

        first_source = tmp_path / "qualification-one"
        first_source.mkdir()
        (first_source / "one.txt").write_text("qualification one\n", encoding="utf-8")
        second_source = tmp_path / "qualification-two"
        second_source.mkdir()
        (second_source / "two.txt").write_text("qualification two\n", encoding="utf-8")

        first = add_archive(store, backend, [first_source], name="qualification-one")
        second = add_archive(store, backend, [second_source], name="qualification-two")
        assert first.tape_file_no == 1
        assert second.tape_file_no == 2

        backend.seek_eod()
        assert backend.current_file_no() == 3
        assert backend.status().file_no == 3

        # A fresh backend models a subsequent process: it must locate each
        # physical file through mt and consume it independently of the writer.
        reopened = LinuxTapeBackend(
            configuration.device,
            mt_program=configuration.mt_program,
        )
        active_backend = reopened
        assert reopened.status().loaded
        for expected_file_no in (1, 2):
            reopened.seek_file(expected_file_no)
            assert reopened.current_file_no() == expected_file_no
            stream = reopened.read_tape_file()
            try:
                assert stream.read(512), f"physical tape file {expected_file_no} is empty"
            finally:
                stream.close()

        verification = verify_tape(store, reopened, configuration.tape_id)
        assert [result.tape_file_no for result in verification] == [1, 2]
        assert all(result.ok for result in verification), verification

        restored_one = restore_archive(store, reopened, first.name, tmp_path / "restore-one")
        restored_two = restore_archive(store, reopened, second.name, tmp_path / "restore-two")
        assert restored_one.extracted_count > 0
        assert restored_two.extracted_count > 0
        assert (tmp_path / "restore-one" / "qualification-one" / "one.txt").read_text(
            encoding="utf-8"
        ) == "qualification one\n"
        assert (tmp_path / "restore-two" / "qualification-two" / "two.txt").read_text(
            encoding="utf-8"
        ) == "qualification two\n"

        reopened.eject()
        ejected = True
        assert not reopened.status().loaded
        qualification_succeeded = True
    finally:
        # Keep a failed qualification loaded for inspection/recovery.  Never
        # attempt rewind/erase/overwrite as cleanup; a failed qualification
        # remains a release blocker and must be investigated manually.
        if qualification_succeeded and not ejected and media_loaded:
            active_backend.eject()
