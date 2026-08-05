# tmux-cli

`tb` — a tmux CLI for humans **and** LLMs. Read, write, create, and manage tmux
sessions from the shell or from a language-model tool-use loop. Tables for
humans; a stable `--json` envelope and stable, distinct exit codes for machines.

Python 3.10+ and stdlib-only (`argparse`, `subprocess`, `urllib`, …) — **no
pip dependencies**. [`zstd`](https://facebook.github.io/zstd/) finalizes and
reads permanent pane logs; if it is temporarily unavailable, plaintext is
preserved for later recovery. The optional
[`ttyd`](https://github.com/tsl0922/ttyd) executable is used by `tb web`.

> Want the web dashboard too — every session as an embedded terminal in your
> browser? See **[tmux-browse](https://github.com/itsmygithubacct/tmux-browse)**,
> which builds on this CLI and pulls it in as a submodule.

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

## For LLM tool-use

Every verb accepts `--json` (a stable `{ok, data}` success / `{ok:false, error,
code, exit}` failure envelope) and returns a stable, distinct exit code per
failure class (see [docs/tb.md](docs/tb.md)). `tb exec` runs a command in a pane
and returns its real exit status and output — the load-bearing verb for agent
loops.

## Tests

```bash
make ci            # python3 -m unittest discover tests
```

## License

[MIT](LICENSE).
