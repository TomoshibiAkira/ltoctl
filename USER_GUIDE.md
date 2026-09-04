# ltoctl user guide

This is the operational manual. Read it before writing a real cartridge.

`ltoctl` manages **personal LTO cold archives** on a single Linux tape drive. It writes ordinary `tar` streams, keeps a readable JSON catalog on disk, and refuses to guess when a write would be unsafe.

It is **not** a backup program. There is no incremental engine, no LTFS, no library robot, and no automatic tape recycling. One archive must fit on one physical tape.

## What you need

- Linux, Python 3.11+
- A standalone LTO drive (LTO-6 is the tested target) on the **non-rewinding** device, usually `/dev/nst0`
- Your user in group `tape` (do not run `ltoctl` with sudo)
- Blank or `ltoctl`-initialized cartridges for writing
- Enough free disk for the catalog (`~/.local/share/ltoctl`) and for restore destinations

## Mental model

Tape is sequential. `ltoctl` never rewrites an archive in place. New archives are appended at the end of recorded data.

An initialized cartridge looks like this:

```text
file 0     tiny identity header (__LTOCTL__/tape.json)
filemark
file 1     first archive tar (descriptor + manifest + payload)
filemark
file 2     second archive tar
filemark
...
EOD
```

Two stores exist:

| Store | Role |
| --- | --- |
| **On tape** | Canonical payload. Recoverable with `mt` and `tar` even if `ltoctl` is gone. |
| **Catalog on disk** | Convenience: tape/archive records, search index, saved plans, write journal. |

Losing the catalog is painful but not fatal. Losing the tape is fatal. Keep both backed up.

**Units vs archives vs tapes**

- A **unit** is one directory or file you treat as indivisible (for example `Photos_2021`).
- An **archive** is one tar stream written as one physical tape file.
- A **tape group** in a plan is “these units go on one cartridge.”
- If a unit is larger than usable capacity, it is **oversized**. v1 will not split it.

Default usable LTO-6 capacity is **2.35 TB** (decimal), a margin under the 2.50 TB native rating. Hardware compression is ignored on purpose.

## One-time setup

### Install

From the project directory, with Python 3.11+:

```bash
python3.12 -m pip install -e '.[dev]'   # or conda env py311
ltoctl --help
```

### Device access

`/dev/nst0` is normally `root:tape` mode `0660`. Add yourself to `tape` **once**, then log in again:

```bash
sudo usermod -aG tape "$USER"
# new login
groups          # must list tape
ls -l /dev/nst0
```

Confirm the drive sees a cartridge:

```bash
ltoctl drive status
```

`loaded=true` means a tape is present. `writable=false` means the tab is write-protected.

### Config (optional)

`~/.config/ltoctl/config.toml`:

```toml
catalog_root = "~/.local/share/ltoctl"
device = "/dev/nst0"
media = "lto6"
log_path = "~/.local/state/ltoctl/ltoctl.log"
```

Overrides, highest first: CLI `--catalog` / `--device`, then `LTOCTL_CATALOG`, `LTOCTL_DEVICE`, `LTOCTL_MEDIA`, `LTOCTL_LOG`, then the TOML file.

## First archive, end to end

Do this with a **blank disposable cartridge** until you trust the workflow.

1. **Plan** what will go on which tape (no drive required):

   ```bash
   ltoctl plan create /data/photos /data/documents --unit-depth 1 --save 2026-09
   ltoctl plan show 2026-09
   ```

   You want **Estimated tapes ≥ 1**. Oversized units are listed but **not packed**; `plan apply` writes the groups that fit and leaves oversized trees for a later plan. If estimated tapes is 0, every unit is larger than one tape; split sources (see [Planning](#planning)).

2. **Load a blank cartridge.** `ltoctl drive status` should show it loaded and writable.

3. **Write group 1** of the saved plan. If the cartridge is blank, apply asks whether to initialize it and which tape ID to put on the label (`HOME-001`, not a random phrase). That writes file 0, then the archives.

   ```bash
   ltoctl plan apply 2026-09 --group 1
   ```

   You can still initialize separately with `ltoctl tape init HOME-001 --media lto6 --yes` if you prefer. JSON/scripted runs **must** pass `--group N`, and a blank cartridge needs `--init-tape HOME-001 --yes`. Interactive apply without `--group` prompts you to load the next tape.

4. **Check**:

   ```bash
   ltoctl archive list
   ltoctl verify tape HOME-001
   ltoctl catalog rebuild-index
   ```

5. **Eject**, label the cartridge with the tape ID, and store it.

   ```bash
   ltoctl tape eject
   ```

To add one named tree without a plan:

```bash
ltoctl archive add /data/photos --name photos-2026
```

That still requires an initialized, matching tape in the drive.

## Planning

`plan create` does **not** write tape. It scans metadata and packs units with first-fit decreasing.

```bash
ltoctl plan create /data/cold --unit-depth 1 --save august-2026
ltoctl plan show august-2026
ltoctl plan list
```

**How units are defined**

| Invocation | Units |
| --- | --- |
| `plan create /data/A /data/B` | Each path is one unit |
| `plan create /data/cold --unit-depth 1` | Each **immediate child** of `/data/cold` is one unit |
| `plan create /data/cold` | The whole tree is **one** unit |

`--unit-depth 0` is the same as “this path is one unit.” Depth 2 uses grandchildren, and so on.

**Read the summary**

- **Estimated tapes** = number of packed groups (cartridges the planner can fill).
- **Oversized units** = units bigger than usable capacity. They are **not** packed, so they do not add to estimated tapes. `plan apply` skips them (plan those trees later). If everything is oversized, estimated tapes is **0** and apply has nothing to write.
- Usable capacity defaults to the media profile (`lto6` → 2.35 TB). Override with `--capacity 2.30TB`.
- `TB` is 10¹² bytes; `TiB` is 2⁴⁰ bytes. Do not mix them.

`--capacity 2.5` means **2.5 bytes**, not 2.5 terabytes. Always include a unit (`2.30TB`, `2350GB`).

**Before apply, freeze the packed sources.** `plan apply` rescans packed units only. If size, file count, mtime, or the snapshot fingerprint of a packed unit changed, it refuses. Recreate the plan; it will not silently reshuffle. Oversized units are ignored at apply so you can plan those trees later.

**Saved plans** live in `~/.local/share/ltoctl/plans/<plan-id>.json`. `--save NAME` is the plan ID.

## Writing to tape

### Initialize (`tape init`)

Use only on a **blank** cartridge (physical EOD at file 0). `--yes` is required for non-interactive use. The command writes the identity header and a catalog tape record.

`plan apply` can do this for you when it sees a blank cartridge: it prompts (or you pass `--init-tape ID --yes`). `tape init` remains the explicit one-shot command. Do not init a tape that already has data you care about.

### Append one archive (`archive add`)

```bash
ltoctl archive add /data/photos --name photos-2026
```

Safety checks before any write: tape loaded, header readable, catalog UUID match, no unresolved journal, append at verified EOD, capacity budget.

### Execute a plan (`plan apply`)

```bash
ltoctl plan apply august-2026                 # interactive, one group after another
ltoctl plan apply august-2026 --group 1       # one group, no swap prompt
```

- Each group binds to the **loaded** tape UUID on first use.
- A blank cartridge (physical EOD at file 0) is offered for initialization instead of requiring a separate `tape init`. Used or corrupt media is never initialized this way.
- Resume requires the **same** tape unless you remap.
- Remap an unfinished group only with `--remap-group N --yes`, and only if that group has no completed units.
- `--json` on an unfinished multi-group plan requires `--group`. A blank cartridge in JSON/scripted apply also needs `--init-tape ID --yes`.
- Oversized units are skipped, not split. Apply succeeds if at least one packed group exists; plan those oversized trees separately when you are ready.

### Mark obsolete

```bash
ltoctl archive mark-obsolete photos-2026
```

This changes catalog status only. Bytes stay on tape. Space is not reclaimed.

### Interrupted writes

If a write dies mid-stream, later appends to that tape are blocked. Inspect and run:

```bash
ltoctl tape reconcile
ltoctl tape reconcile --operation OPERATION_UUID
```

Reconcile will commit catalog metadata if a complete archive is on tape. If the last file looks incomplete, the tape is marked `needs_recovery`. v1 will not erase or skip a damaged file for you.

## Finding, restoring, verifying

Rebuild the search index after new archives:

```bash
ltoctl catalog rebuild-index
ltoctl search IMG_1234
ltoctl search 'IMG_.*' --regex --archive photos-2026
```

Search uses `index/files.tsv`. Deleting that file is safe; rebuild it. Default search is a case-insensitive substring.

Restore (does not overwrite existing files unless `--overwrite`):

```bash
ltoctl restore photos-2026 --output /restore/photos
ltoctl restore photos-2026 --output /restore/one --file photos-2026/readme.txt
```

Selected-file restore still reads the archive sequentially. Load the correct cartridge when asked.

Verify hashes the exact tar bytes on tape:

```bash
ltoctl verify archive photos-2026
ltoctl verify tape HOME-001
```

Failures are reported; nothing is deleted. Marking an archive `corrupt` is a separate catalog decision.

## Catalog maintenance

```bash
ltoctl catalog validate
ltoctl catalog rebuild-index
ltoctl catalog export --output catalog.txt
```

`validate` checks references, UUIDs, file-number collisions, and manifests. `export` is a human-readable dump, not canonical state.

Catalog files (do not edit by hand unless you know the schemas):

```text
~/.local/share/ltoctl/
  tapes/<tape-id>.json
  archives/<uuid>.json
  manifests/<uuid>.jsonl
  plans/<plan-id>.json
  operations/<operation-uuid>.json
  index/files.tsv          # derived
```

## Command map

Global options: `--catalog DIR`, `--device /dev/nst0`. Most commands accept `--json`.

| Goal | Command |
| --- | --- |
| Drive loaded / writable? | `ltoctl drive status` |
| List known tapes | `ltoctl tape list` |
| Show one tape | `ltoctl tape info HOME-001` |
| Blank tape → identity | `ltoctl tape init HOME-001 --media lto6 --yes` · or `plan apply --init-tape HOME-001 --yes` |
| Fix interrupted write | `ltoctl tape reconcile` |
| Unload | `ltoctl tape eject` |
| Pack sources | `ltoctl plan create PATHS --unit-depth 1 --save NAME` |
| Inspect / apply plan | `ltoctl plan show NAME` · `ltoctl plan apply NAME` |
| One-off archive | `ltoctl archive add PATH --name NAME` |
| List / obsolete | `ltoctl archive list` · `ltoctl archive mark-obsolete NAME` |
| Find a file | `ltoctl search QUERY` |
| Restore | `ltoctl restore NAME --output DIR` |
| Verify | `ltoctl verify archive NAME` · `ltoctl verify tape ID` |
| Catalog health | `ltoctl catalog validate` · `rebuild-index` · `export` |

Exit codes: `0` ok, `2` usage/catalog/plan error, `3` safety stop, `4` tape/archive/verify I/O error. A group with no subcommand prints that group's help and exits `2`.

## What not to do

- Do not run `ltoctl` as root to “fix permissions.” Join group `tape`.
- Do not use `/dev/st0` (rewinding) for normal operations; use `/dev/nst0`.
- Do not `tape init` a cartridge that already holds archives you need.
- Do not expect `mark-obsolete` to free tape space.
- Do not change source trees after `plan create` and before `plan apply`.
- Do not pass `--capacity 2.5` when you mean 2.5 TB.
- Do not treat a successful mock test as hardware proof. The opt-in `pytest -m tape_hardware` run is the qualification (see [README](README.md)).
- Do not long-erase LTO as a routine reset; it can take hours. Prefer a blank cartridge, or a short erase from BOT (`mt -f /dev/nst0 rewind && mt -f /dev/nst0 erase 0`) only on media you accept destroying.

## Troubleshooting

**Permission denied on `/dev/nst0`**  
You are not in group `tape`, or this login predates `usermod`. Check `groups` and `ls -l /dev/nst0`.

**`drive status` / `mt`: no medium / not ready**  
The cartridge is ejected or still threading. Wait for the drive to go ready, then retry. Do not start a write until `ltoctl drive status` shows loaded.

**Estimated tapes is 0**  
Every unit exceeds usable capacity, so there are no packed groups. Use `--unit-depth 1` (or deeper) or pass smaller roots. `plan apply` refuses only when there is nothing packed to write.

**Plan apply: source snapshot changed**  
The tree changed since planning. Recreate the plan from current sources.

**Append blocked: unresolved operation**  
`ltoctl tape reconcile`. If the tape is `needs_recovery`, stop writing and recover manually.

**Tape ID / UUID mismatch**  
The loaded cartridge is not the one in the catalog (or not the one this plan group bound). Load the labeled tape, or remap an unfinished group explicitly.

**Init refuses: tape is not blank**  
Physical EOD is not file 0. Use a blank cartridge. v1 will not overwrite file 0 on a used tape.

**Restore path already exists**  
Default is no overwrite. Choose an empty `--output` or pass `--overwrite`.

## Recovery without ltoctl

On the non-rewinding device:

```bash
mt -f /dev/nst0 rewind
tar -tvf /dev/nst0          # file 0: identity header
mt -f /dev/nst0 rewind
mt -f /dev/nst0 fsf 1
tar -tvf /dev/nst0          # first archive
```

File 0 contains `__LTOCTL__/tape.json`. Archive files start with `__LTOCTL__/archive.json` and `__LTOCTL__/manifest.jsonl`, then payload. After restoring a catalog copy, run `ltoctl catalog validate`.
