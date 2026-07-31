# Permanent tmux-cli session logs

tmux-cli records each configured tmux pane as a permanent, lossless sequence of
Zstandard frames. Completed segments use `zstd -3` and live below:

```text
~/.gpu_terminal/tmux-cli/logs/
```

tmux-cli does not automatically delete completed archives. Session kill,
rename, pane exit, tmux server exit, updates, and `make uninstall` leave them in
place. “Permanent” here means retained until the user explicitly removes them;
the directory is local data, not a substitute for a filesystem backup.

The copy you are reading is installed from the repository's
`docs/logging.md`. Its installed location is:

```text
~/.gpu_terminal/tmux-cli/logging.md
```

## Quick reference

```bash
tb logs                         # newest-first capture list
tb logs list --session work     # only one session name
tb logs show CAPTURE            # reconstruct one pane stream
tb logs grep -i 'migration'     # search every reconstructed stream
tb logs path                    # print ~/.gpu_terminal/tmux-cli
tb logs path CAPTURE            # print that capture's segment paths
tb logs verify [CAPTURE]        # validate zstd frames and sequence order
tb logs recover                 # recover plaintext and import legacy logs
tb logs manual                  # print this installed manual's path
```

Capture IDs may be abbreviated to any unambiguous prefix.

Every command supports the normal global flags. In particular:

```bash
tb logs --json
tb logs show CAPTURE --json     # content is returned as base64
tb logs grep 'needle' --json
```

`tb logs grep` exits 1 when it finds no matches, like ordinary grep.

## Storage layout

```text
~/.gpu_terminal/tmux-cli/
├── logging.md
├── logs/
│   ├── live/
│   │   └── <capture-id>--<sequence>.log
│   ├── archive/
│   │   └── YYYY/MM/DD/
│   │       └── <started-utc>--<session>--p<pane>--<capture-id>--<sequence>.log.zst
│   └── metadata/
│       └── <capture-id>.json
├── runtime/
│   ├── activity/
│   │   └── <encoded-session>.log
│   └── logging-errors.log
└── locks/
```

`logs/archive` is permanent history. The other areas are recoverable working
state:

- `logs/live` contains the current plaintext segment or plaintext retained
  after a compression failure.
- `logs/metadata` maps random capture IDs to session name, pane ID, initial
  working directory and command, timestamps, and segment names.
- `runtime/activity` is a bounded tail used only for content-based idle
  detection.
- `runtime/logging-errors.log` records owner-only compression and recovery
  failures.
- `locks` prevents a recovery pass from touching live writers.

Directories are mode `0700`; archives, metadata, runtime files, locks, and this
manual are mode `0600`.

## What is captured

tmux `pipe-pane` supplies pane output. tmux-cli records those bytes exactly,
including ANSI control sequences, carriage returns, NUL bytes, and invalid
UTF-8.

Typed input appears only if the application or terminal echoes it. A password
entered while terminal echo is disabled is not recorded. Programs can still
print secrets, tokens, environment values, or other sensitive information, so
the archive should be treated as private.

Output from different panes is kept in different captures. tmux does not
provide a trustworthy total order across panes, and separate writers avoid
cross-pane finalization races. Metadata and `tb logs list --session NAME` group
the pane captures belonging to a session.

Logging begins when tmux-cli configures `pipe-pane`; it does not retroactively
copy older tmux scrollback.

## Compression and permanence

One writer owns one pane capture. It writes two destinations:

1. A complete per-pane capture split at
   `TB_SESSION_LOG_SEGMENT_BYTES`—8 MiB by default.
2. A disposable per-session activity tail used by `tb wait` and the dashboard.

When a capture segment reaches its boundary, or when the pane pipe reaches EOF,
the writer:

1. closes and syncs the plaintext segment;
2. runs `zstd -3` into a temporary file in the final archive directory;
3. validates the frame with `zstd -t`;
4. decompresses it and verifies an exact byte-for-byte source match;
5. applies mode `0600` and preserves the source modification time;
6. atomically publishes the `.log.zst`; and
7. deletes the plaintext only after publication succeeds.

Rollover never discards output. It archives the complete segment and opens the
next sequence. Concatenating the decompressed segments reproduces the exact
byte stream accepted by that pane's writer.

If `zstd` is missing, the disk is full, compression fails, or validation fails,
the plaintext remains in `logs/live`. A later `tb logs recover` retries it.
tmux lifecycle commands still report what tmux did; logging errors are reported
separately and never turn into silent deletion.

The old `TB_SESSION_LOG_MAX_BYTES` variable still controls only the disposable
activity tail. It defaults to 10 MiB and retains its newest 8 MiB. Permanent
segment size is configured separately:

```bash
export TB_SESSION_LOG_SEGMENT_BYTES=$((8 * 1024 * 1024))
```

Set the variable in the environment of the process that calls
`ensure_logging`; existing pane writers retain the value with which they
started.

## Reading logs with tmux-cli

List captures:

```bash
tb logs
tb logs list --session work
tb logs list --pane %12
```

The list includes:

- `live`: a pane writer currently owns the capture;
- `pending`: plaintext awaits safe recovery;
- `closed`: all known segments are archived.

Read one capture, with its segments transparently decompressed in order:

```bash
tb logs show 63ec19baaa5f
tb logs show 63ec19baaa5f | less -R
```

Search reconstructed captures:

```bash
tb logs grep -i 'migration'
tb logs grep -F 'git filter-repo' --session work
tb logs grep 'error|failed' --capture 63ec19baaa5f
```

`tb logs grep` reconstructs the pane stream before matching. This matters when
a word or line happens to cross a segment boundary.

Terminal streams contain control sequences. Only display raw output from panes
you trust. If `ansifilter` is installed, a text-only view is easier to read and
safer to display:

```bash
tb logs show 63ec19baaa5f | ansifilter --text | less
```

## Reading and grepping zstd files directly

Set a short variable for these examples:

```bash
TB_LOG_ROOT="$HOME/.gpu_terminal/tmux-cli"
```

List completed archives:

```bash
find "$TB_LOG_ROOT/logs/archive" -type f -name '*.log.zst' \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS  %10s  %p\n' | sort -r
```

Read one segment:

```bash
zstd -dcq -- "/path/to/segment.log.zst" | less -R
zstdcat -- "/path/to/segment.log.zst" | less -R
```

Search one segment:

```bash
zstdgrep -a -n -i -- 'migration' "/path/to/segment.log.zst"
```

Search every archive with ripgrep's Zstandard support:

```bash
rg -z -a -n -i --glob '*.log.zst' -- 'migration' \
  "$TB_LOG_ROOT/logs/archive"
```

Use `-F` for a literal string:

```bash
rg -z -a -n -F -- 'git filter-repo' "$TB_LOG_ROOT/logs/archive"
```

Direct per-file search can miss a match split across two segments. Use
`tb logs grep` when that boundary case matters.

Search pending plaintext:

```bash
rg -a -n -i -- 'migration' "$TB_LOG_ROOT/logs/live"
```

ANSI sequences can occur between visibly adjacent characters. Normalize a
segment before searching if needed:

```bash
zstd -dcq -- "/path/to/segment.log.zst" |
  ansifilter --text |
  rg -n -i -- 'migration'
```

Reassemble one capture directly. Sequence numbers are zero-padded, so sorting
matching names puts them in byte order:

```bash
find "$TB_LOG_ROOT/logs/archive" -type f \
  -name '*--63ec19baaa5f4a83886ef93cc6250f15--*.log.zst' \
  -print0 |
  sort -z |
  xargs -0 -r zstd -dcq --
```

Create a private plaintext copy:

```bash
outfile="./recovered-session.log"
(umask 077; tb logs show 63ec19baaa5f > "$outfile")
```

## Integrity and disk use

Verify through tmux-cli:

```bash
tb logs verify
tb logs verify 63ec19baaa5f
```

Verify one frame directly:

```bash
zstd -tq -- "/path/to/segment.log.zst"
```

Verify every frame:

```bash
find "$TB_LOG_ROOT/logs/archive" -type f -name '*.log.zst' \
  -exec zstd -tq -- {} +
```

Inspect a frame and total storage:

```bash
zstd -lv -- "/path/to/segment.log.zst"
du -sh "$TB_LOG_ROOT/logs"
```

There is intentionally no automatic purge. Deletion is an explicit operator
action against resolved archive paths.

## Recovery and legacy migration

Run:

```bash
tb logs recover
```

Recovery takes a nonblocking global lock. It never compresses a capture whose
writer lock is held. An unlocked plaintext spool older than the safety grace
period is compressed and published with the normal atomic procedure.

The same command imports old session logs from:

```text
~/.tmux-browse/session-logs/
```

An active legacy log is left alone until the pane has been rewired to the new
writer and the old file has stopped changing. Orphaned legacy files are
assigned permanent capture IDs, marked `legacy-tmux-browse` in metadata, and
copied into validated archives. The legacy plaintext source and its marker are
deliberately retained; tmux-cli never deletes them as part of migration.

Normal maintenance recovers abandoned new-format spools but never imports or
alters legacy plaintext. `tb logs recover` performs the complete available
legacy import pass. To recover only new-format spools:

```bash
tb logs recover --no-legacy
```

Unrelated `~/.tmux-browse/agent-logs` and `agent-conversations` are not pane
output and are not migrated.

## Installation

Repository installs copy, rather than symlink, this file:

```text
tmux-cli/docs/logging.md
    -> ~/.gpu_terminal/tmux-cli/logging.md
```

Both `make install` and `bin/update_tb.py` install the manual. `make uninstall`
removes only the executable link; it does not remove the manual, metadata,
pending plaintext, or archives.

`zstd` is required to finalize and read completed archives. tmux-cli itself
continues to use only the Python standard library.
