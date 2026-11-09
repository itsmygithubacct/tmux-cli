# tmux-cli

`tb` — a tmux CLI for humans **and** LLMs. Read, write, create, and manage tmux
sessions from the shell or from a language-model tool-use loop. Tables for
humans; a stable `--json` envelope and stable, distinct exit codes for machines.

**Status:** ships in Plebian-OS 0.2.3 inside Tmux Manager: it is a submodule
of [tmux-tui](https://github.com/itsmygithubacct/tmux-tui#readme), which Pleb
installs at first boot through Kilix (`kilix tmux --install-only --with-tb`),
publishing this repository's `tb.py` as the `tb` command. It is also a
submodule of [tmux-browse](https://github.com/itsmygithubacct/tmux-browse#readme),
and Kilix's own `kilix tmux --socket` backend is a vendored derivative of its
session and targeting code.

Python 3.10+ and stdlib-only (`argparse`, `subprocess`, `urllib`, …) — **no
pip dependencies**. [`zstd`](https://facebook.github.io/zstd/) finalizes and
reads permanent pane logs; if it is temporarily unavailable, plaintext is
preserved for later recovery. The optional
[`ttyd`](https://github.com/tsl0922/ttyd) executable is used by `tb web`.

> Want the web dashboard too — every session as an embedded terminal in your
> browser? See **[tmux-browse](https://github.com/itsmygithubacct/tmux-browse)**,
> which builds on this CLI and pulls it in as a submodule.

## What it does

- Lists, creates, renames, attaches to and kills tmux sessions without
  attaching first.
- Reads pane scrollback, follows output, waits for a pane to go idle and
  streams activity events.
- Sends literal text, typed lines, named keys and pasted stdin to exact
  `session[:window[.pane]]` targets.
- Runs a command in a pane and returns its real exit status and output
  (`tb exec`).
- Records configured panes as permanent, lossless zstd logs and searches them
  (`tb logs`).
- Controls the dashboard's `ttyd` for a session (`tb web`) and dumps full state
  for polling UIs (`tb snapshot`).

## Watch it

https://github.com/user-attachments/assets/b8e9d432-a0c3-4ede-9cdb-1f1d556c6b63

**[tmux-cli: tmux for humans and LLMs](https://github.com/itsmygithubacct/tmux-cli/releases/download/media-v1/tmux-cli.mp4)**
— a two-minute tour on a real server: session basics without attaching, the
agent verbs and the JSON envelope, and the permanent zstd pane logs
(1920×1080, 2m18s, 8 MB; published as a
[media release](https://github.com/itsmygithubacct/tmux-cli/releases/tag/media-v1)
so a clone stays small).

## Install

Clone and symlink:

```bash
git clone https://github.com/itsmygithubacct/tmux-cli.git ~/tmux-cli
cd ~/tmux-cli && make install      # links tb and installs the logging manual
tb ls
```

Or pull just the files (no git) with the standalone updater:

```bash
curl -fsSL https://raw.githubusercontent.com/itsmygithubacct/tmux-cli/main/bin/update_tb.py -o update_tb.py
python3 update_tb.py --dir ~/bin/tmux-cli
```

Both install paths copy [the permanent-log manual](docs/logging.md) to
`~/.gpu_terminal/tmux-cli/logging.md`.

## Quick start

```bash
tb new work                       # create a detached session
tb type work "make test"          # run a line in it
tb wait work --idle 3             # block until the pane goes quiet
tb capture work -n 200            # read the scrollback
tb exec work --json -- pytest -q  # run a command, get {ok, exit_status, output}
tb logs grep -i "failed"           # search permanent zstd pane history
tb ls --json                      # machine-readable session list
tb snapshot --tmux-only --capture work:0.0  # state + preview for polling UIs
```

`tb --help` lists every verb. See the **[full CLI reference](docs/tb.md)** and
the **[permanent logging guide](docs/logging.md)**.

## Usage

| Group | Verbs |
|---|---|
| Read | `ls`, `show`, `capture`, `tail`, `exists` |
| Write | `send`, `type`, `key`, `paste`, `exec` |
| Lifecycle | `new`, `range`, `stagger`, `kill`, `rename`, `attach`, `a` |
| Observe | `wait`, `watch`, `snapshot`, `describe` |
| Dashboard | `web`, `config` |
| Permanent logs | `logs` |

Global flags (before or after the verb): `--json`, `--quiet`/`-q`,
`--no-header`, `--version`. Destructive verbs require `-f`/`--force`.

## For LLM tool-use

Every verb accepts `--json` (a stable `{ok, data}` success / `{ok:false, error,
code, exit}` failure envelope) and returns a stable, distinct exit code per
failure class (see [docs/tb.md](docs/tb.md)). `tb exec` runs a command in a pane
and returns its real exit status and output — the load-bearing verb for agent
loops.

## How it works

`tb.py` is a thin entry point over `lib/`. `lib/sessions.py` and
`lib/targeting.py` wrap the `tmux` executable and resolve exact targets;
`lib/output.py` and `lib/errors.py` produce the JSON envelope and the fixed
exit codes; the verb groups live in `lib/tb_cmds/`. Permanent logs use tmux
`pipe-pane`: `lib/session_log_writer.py` spools each pane's bytes and archives
every completed spool with `zstd -3`, deleting plaintext only after the frame
verifies. `lib/ttyd.py` and `lib/extensions/` are shared with tmux-browse,
which loads this repository as its CLI core.

## Tests

```bash
make ci            # python3 -m unittest discover tests
```

## Documentation

- [docs/tb.md](docs/tb.md) — full CLI reference: targets, exit codes, JSON envelope, every verb, environment variables
- [docs/logging.md](docs/logging.md) — permanent zstd pane logs: layout, retention, `tb logs`

## Related repositories

- [tmux-tui](https://github.com/itsmygithubacct/tmux-tui#readme) — the Tmux Manager TUI built on this CLI (submodule)
- [tmux-browse](https://github.com/itsmygithubacct/tmux-browse#readme) — web dashboard that embeds each session through `ttyd` (submodule)
- [kilix-tmux](https://github.com/itsmygithubacct/kilix-tmux#readme) — socket-explicit backend derived from this CLI's core
- [kilix](https://github.com/itsmygithubacct/kilix#readme) — installs tmux-tui and this CLI at pinned commits

## Part of Plebian-OS

tmux-cli is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It is installed at first boot as tmux-tui's submodule and published as `tb`.
The map of every repository and how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

[MIT](LICENSE).
