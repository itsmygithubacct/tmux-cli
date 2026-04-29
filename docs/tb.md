# `tb` — tmux CLI for humans and LLMs

`tb` is a single-binary command-line companion to the dashboard. It shares
all the same library code and is deliberately shaped so a shell user and a
language-model tool-use loop both feel at home.

**Design contract:**

- Auto-detects TTY. Tables with colour on a terminal; plain/JSON when piped.
- `--json` flag on every verb emits a **stable** envelope (see below).
- Exit codes are **stable** and distinct per failure class.
- No interactive prompts. Destructive ops require `-f`/`--force`.
- Verbs are **idempotent** where they can be (`--json` mode especially):
  `kill` of a dead session succeeds quietly; `new` on an existing name
  errors out clearly; nothing requires "check-then-act" round-trips.
- Deterministic targeting: always `session[:window[.pane]]`, never an
  implicit "last session".

## Running

```bash
python3 tb.py <verb> [args]
# or
chmod +x tb.py && ln -s $PWD/tb.py ~/bin/tb
tb <verb> [args]
```

## Global flags

Accepted both before **and** after the verb (shared parent parser):

| Flag | Effect |
|---|---|
| `--json` | emit JSON; envelope is `{ok: true, data}` or `{ok: false, error, code, exit}` |
| `--quiet`, `-q` | suppress non-error output on success |
| `--no-header` | for table verbs, skip the header row |
| `--version` | print version |
| `-h`, `--help` | help (also per-verb: `tb VERB --help`) |

## Targets

Target syntax is uniform: `session`, `session:window`, or
`session:window.pane`. Every verb that takes a target accepts any of those
three forms. Most verbs operate on the session's active pane when the
target doesn't specify one.

Session names cannot contain whitespace, `:`, or `.` — `tb new` enforces
this.

## Exit codes

| Code | Symbol | Meaning |
|---:|---|---|
| 0 | — | success |
| 1 | — | generic error |
| 2 | `EUSAGE` | usage error (bad args/flags) |
| 3 | `ENOENT` | session not found |
| 4 | `EEXIST` | session already exists |
| 5 | `ETIMEDOUT` | operation timed out |
| 6 | `ENOSERVER` | no tmux server is running |
| 7 | `ETMUX` | tmux command failed unexpectedly |
| 8 | `ESTATE` | ``~/.tmux-browse`` state is corrupt or unwritable |
| 9 | `EAUTH` | dashboard auth failed (dashboard-only, not `tb`) |
| 130 | — | interrupted (SIGINT / Ctrl-C) |

`ls`, `exists`, and `snapshot` are deliberately exempt from the
"no tmux server" short-circuit — they have meaningful zero-session
behaviour (empty list / exit 3 / empty payload).

## JSON envelope

Success:

```json
{"ok": true, "data": <verb-specific payload>}
```

Failure (emitted to `stderr`):

```json
{"ok": false, "error": "no such session: foo", "code": "ENOENT", "exit": 3}
```

`code` is stable. `error` is the human message and may change phrasing
between versions.

## Verbs

### Read

#### `tb ls [--running] [--attached]`

Table of sessions: name, windows, attach count, idle, created-ago.
`--running` limits to sessions with activity in the last 30 s;
`--attached` limits to those with ≥ 1 attached client.

```
$ tb ls
SESSION   WIN  ATT  IDLE  CREATED
notes     1    0    5m    1d ago
work      1    1    12s   2h ago
```

#### `tb show <target>`

One session with its panes as a table (window, pane, window name, current
command, pid, cwd, active flag).

```
$ tb show work
work   1 windows, 1 attached
W  P  WINDOW-NAME  CMD   PID    CWD            ACTIVE
0  0  bash         vim   12345  /home/u/proj   True
```

#### `tb capture <target> [-n LINES] [--ansi]`

Dumps `tmux capture-pane` output as plain text. `--ansi` preserves escape
sequences (`tmux -e`). Default lines: 2000.

```bash
tb capture work -n 500 > /tmp/work.log
tb capture work --json | jq -r .data.content
```

#### `tb tail <target> [-n LINES] [-f] [--interval SEC]`

Print recent pane content; `-f` polls every `--interval` seconds (default
0.5) and appends new suffix lines. Ctrl-C to stop. If the pane redraws
(cleared, scrolled far), prints `--- pane redrew ---` and replays.

#### `tb exists <target>`

Silent. Exit 0 if session exists, exit 3 if not. The cheapest existence
probe.

```bash
tb exists work && tb exec work -- ./run_tests.sh
```

### Config

#### `tb config [show]`

Prints the current dashboard config from
`~/.tmux-browse/dashboard-config.json`. In plain mode it renders a two-column
table; in `--json` mode it emits `{path, config}`.

```bash
tb config
tb config --json
```

#### `tb config get <key>`

Prints one config value. Useful in shell scripts and for checking the current
agent step budget.

```bash
tb config get agent_max_steps
```

#### `tb config set <key> <value>`

Updates one key in `dashboard-config.json`, then normalizes and persists the
whole file through the same rules the dashboard uses. Booleans accept common
string forms such as `true`, `false`, `on`, and `off`.

```bash
tb config set agent_max_steps 20
tb config set auto_refresh true
```

#### `tb config reset`

Writes the built-in dashboard defaults back to
`~/.tmux-browse/dashboard-config.json`.

```bash
tb config reset
```

### Write

#### `tb send <target> <text...>`

Sends literal keystrokes, **without Enter**. Multiple args are joined by a
single space (tabs, embedded newlines, or exact whitespace are lost in the
join — use `tb paste` for anything non-trivial). `tmux send-keys -l`
under the hood.

```bash
tb send work "echo hi"      # types 'echo hi' — still at the prompt
tb send work "echo hi" "x"  # types 'echo hi x'
```

#### `tb type <target> <text>`

`send` + `Enter`. The "run this one line" convenience.

```bash
tb type work "cd /tmp && ls"
```

#### `tb key <target> <key...>`

Sends one or more tmux key names. Common names: `Enter`, `Escape`, `Tab`,
`BSpace`, `Space`, `Up`, `Down`, `Left`, `Right`, `Home`, `End`, `PageUp`,
`PageDown`, `C-a`..`C-z`, `M-a`..`M-z`, `F1`..`F12`.

```bash
tb key work C-c             # send SIGINT
tb key work Escape : q Enter  # :q in vim
```

#### `tb paste <target>`

Reads stdin, loads it into a tmux buffer, and pastes it into the pane with
`paste-buffer -d`. Preserves newlines; better than `send` for multi-line
blobs or text with special characters. Refuses to run with stdin attached
to a TTY (would hang forever).

```bash
tb paste work < big_script.txt
echo -e "line1\nline2" | tb paste work
```

#### `tb exec <target> [--timeout N] [--strategy S] -- <cmd...>`

**The load-bearing verb for LLM workflows.** Runs a command in the pane,
waits for it to complete, returns its output and exit status.

```bash
$ tb exec work --timeout 10 -- ls -1 | head -3
Cargo.toml
README.md
src

$ tb exec work --json --timeout 30 -- "pytest -q tests/core"
{"ok": true, "data": {
  "ok": true, "exit_status": 0, "output": "…44 passed…", "duration": 12.3
}}
```

**Two strategies**, auto-selected from `pane_current_command`:

- **Sentinel** (default when the pane is running a shell): wraps the
  command in `printf` markers and waits for the END sentinel to appear in
  the scrollback. Returns the real `exit_status` and the output captured
  between the markers. Reliable — "saw the sentinel, we're done."
- **Idle** (used for non-shell panes, e.g. a REPL): sends the command,
  polls until the pane has been silent for `--idle-sec` seconds (default 2),
  returns the captured diff. `exit_status` is `null` in this mode —
  silence isn't a proof of completion for backgrounded work.

Force one with `--strategy {sentinel,idle}`. `--timeout` is the overall
deadline; on timeout exits 5 (`ETIMEDOUT`) and, by default, sends `C-c` to
the pane so the orphaned command doesn't keep emitting sentinels into the
next `exec` call. Use `--no-interrupt` for fire-and-leave semantics.

`--clear` sends `C-u C-k` before the wrapper, resetting any half-typed
readline buffer. Recommended in LLM loops where you can't be sure the pane
was sitting at a clean prompt.

Caveat: `&`-backgrounded commands always return `exit_status: 0` in
sentinel mode — `$?` after the fork is the shell's view, not the job's.
Idle mode returns `null` for such commands (more honest).

Use `--` to separate the command from tb flags:

```bash
tb exec work --timeout 5 --clear -- pytest -x --lf
tb exec work --json --strategy idle --idle-sec 5 -- python repl.py
tb exec work --timeout 60 --no-interrupt -- long-build.sh  # keep running past deadline
```

### Lifecycle

#### `tb new [name] [--cwd DIR] [--cmd CMD] [--auto] [--attach]`

Creates a detached session. `--auto` generates a random name (prints it on
stdout for capture). `--cwd` sets the starting directory; `--cmd` runs a
command in the first pane. `--attach` `exec()`s into `tmux attach` — only
valid from a TTY.

```bash
tb new work                           # plain
tb new build --cwd ~/src/proj --cmd "make watch"
NAME=$(tb new --auto)                 # for scripts/agents
```

Exits 4 (`EEXIST`) if the name is taken; 2 (`EUSAGE`) for invalid names.

#### `tb kill <target> [-f]`

Kills the tmux session. Requires `-f`/`--force` when stdout is a TTY.
When stdout is **not** a TTY (piped / in a script), no confirmation is
needed. With `--json`, `kill` of a non-existent session is idempotent and
returns `{already_gone: true}` instead of error.

#### `tb rename <old> <new>`

Renames a session.

#### `tb attach <target>`

`exec()`s into `tmux attach-session`. Only useful from an interactive TTY.

### Observe

#### `tb wait <target> [--idle SEC] [--timeout SEC]`

Blocks until the pane has been silent (capture unchanged) for `--idle`
seconds. Default idle: 2. `--timeout 0` (default) means no timeout; any
positive value exits 5 on expiry. Pairs nicely with `send` to say "fire
command, wait for the shell to be ready again, read."

```bash
tb type work "make test" && tb wait work --idle 3 --timeout 300
tb capture work -n 200
```

#### `tb watch <target> [--interval SEC]`

Streams change events to stdout. In default mode, prints a timestamp and
the pane's last line whenever output changes. With `--json`, emits one
JSON object per event (**newline-delimited JSON, not the `{ok, data}`
envelope** — streaming consumers find NDJSON easier). Ctrl-C to stop.

```
$ tb watch work
[14:02:11] Running tests…
[14:02:14] ok 1 - foo
[14:02:14] ok 2 - bar
```

### Web (dashboard-integrated)

#### `tb web start <session>`

Ensures the dashboard's ttyd is running for this session. Idempotent.
Prints the URL (or the JSON payload). Uses the same `~/.tmux-browse/ports.json`
registry as the dashboard, so the port is stable. In `--json` mode the payload
includes `session`, `port`, `pid`, `already`, `scheme`, and `url`, so callers
do not need to guess whether the ttyd endpoint is `http` or `https`.

#### `tb web stop <session>`

Stops the ttyd. The tmux session is untouched.

#### `tb web url <session>`

Prints the ttyd URL for this session (if a port is assigned). Does not
start ttyd. Host defaults to `localhost`; override with `TB_DASHBOARD_HOST`.
The emitted scheme follows the stored ttyd state, so HTTPS dashboards return
HTTPS ttyd URLs as well.

### Agent (extension)

The full `tb agent ...` verb tree — `add`, `ls`, `remove`, `repl`,
one-shot prompt loop, `cycle`, `work`, sandbox modes — is contributed
by the
[tmux-browse-agent](https://github.com/itsmygithubacct/tmux-browse-agent)
extension. Install it from the dashboard's **Config > Extensions** or
with `make install-agent` on a headless host. Until installed,
`tb agent` errors with "unknown verb". See the extension's README for
the full surface.

### Bulk (for LLM context)

#### `tb snapshot [--human]`

One call, all the state: sessions, per-pane info, ttyd assignments, port
range, dashboard status, tmux server state. Default output is JSON so an
agent can consume it in one go.

```json
{"ok": true, "data": {
  "now": "2026-04-20T…Z",
  "host": "…",
  "tmux_server": true,
  "sessions":  [ … ],
  "panes":     [ … ],
  "ttyd":      { "assignments": {…}, "running": [ … ], "port_range": [7700, 7799] },
  "dashboard": { "listening": true,  "port": 8096 }
}}
```

`--human` prints a four-line summary instead.

#### `tb describe <target>`

Prose summary suitable for LLM context:

```
$ tb describe work
Session 'work': 1 windows, 1 attached, idle 12s.
  * 0.0 bash  cmd=vim  pid=12345  cwd=/home/u/proj
ttyd: port 7702, running (pid 881122)
```

With `--json`, returns both the structured data and the rendered text.

## Environment variables

| Variable | Effect |
|---|---|
| `TB_DASHBOARD_HOST` | host used when building URLs in `tb web url/start` (default `localhost`) |
| `TB_COLOR` | `always` / `never` to override TTY detection for colour output |
| `NO_COLOR` | if set (any value), disables colour |

## Troubleshooting

- **`tb: no tmux server is running` (exit 6).** No tmux sessions exist at all.
  Start one with `tmux new -d -s work` or `tb new work`.
- **`tb: exec timed out after Ns waiting for END sentinel` (exit 5).** The
  command is still running, or the pane isn't a shell. Increase `--timeout`,
  or switch to `--strategy idle`.
- **`tb paste` output looks weird / mangled.** Terminal may have
  bracketed-paste or auto-indent enabled. `tb paste` uses `load-buffer` +
  `paste-buffer -d`, which is usually clean; but if your shell hooks
  intercept paste, try `tb type` one line at a time instead.
- **`tb exec` returned `exit_status: null`.** That means the idle strategy
  was used (the pane wasn't a shell when the command started). Output is
  the captured diff. Force sentinel with `--strategy sentinel` if the pane
  actually is a shell.
- **Port collision when starting a ttyd.** `tb web start` refuses to stomp
  a port already in use and returns `{note: "port already in use"}`. Kill
  the offender or pick a different range in `lib/config.py`.

## See also

- [docs/recipes.md](recipes.md) — practical human and LLM recipes.
- [docs/dashboard.md](dashboard.md) — the web dashboard that shares this library.
- [docs/architecture.md](architecture.md) — design decisions.
