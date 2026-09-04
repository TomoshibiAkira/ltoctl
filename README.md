# ltoctl

Transparent, safety-first CLI for personal LTO cold archives. Payloads are
ordinary streaming tar files. Canonical metadata is JSON/JSONL plus a
rebuildable TSV search index. Tape bytes stay recoverable with `mt` and `tar`
if `ltoctl` itself is unavailable.

**Start here:** [USER_GUIDE.md](USER_GUIDE.md) — setup, first archive, planning,
safety, and troubleshooting.

## Installation

Python 3.11 or newer. From this directory:

```bash
python3 -m pip install -e '.[dev]'
ltoctl --help
```

Typer and Rich are required for the CLI. Without them, `ltoctl` prints an
install hint and exits. The library and mock tape backend remain importable
without a drive.

Default catalog: `~/.local/share/ltoctl`. Default device: `/dev/nst0`.
Default media: `lto6`. Optional config: `~/.config/ltoctl/config.toml`.
Join group `tape` instead of using sudo; details are in the user guide.

## Hardware qualification (developers)

Linux writes in the production CLI use the close-only filemark path after
qualification on real LTO-6 media. Direct `LinuxTapeBackend(...)` construction
stays write-locked unless `allow_unvalidated_write=True`. Mock success is not
qualification. The opt-in test is destructive and excluded by default:

```bash
LTOCTL_TAPE_HARDWARE_ENABLE=1 \
LTOCTL_TAPE_HARDWARE_ACK=I_UNDERSTAND_THIS_IS_A_DISPOSABLE_TAPE \
LTOCTL_TAPE_HARDWARE_DEVICE=/dev/nst0 \
LTOCTL_TAPE_HARDWARE_TAPE_ID=QUAL-LTO6-001 \
PYTHONPATH=. python -m pytest -m tape_hardware -q
```

All four guards are required. There is no fallback to `/dev/nst0`. The tape
must be disposable. Ordinary tests use `-m 'not tape_hardware'`.
