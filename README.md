# tmux-cli

`tb` — a tmux CLI for humans **and** LLMs. Read, write, create, and manage tmux
sessions from the shell or from a language-model tool-use loop. Tables for
humans; a stable `--json` envelope and stable, distinct exit codes for machines.

Python 3.10+ and stdlib-only (`argparse`, `subprocess`, `urllib`, …) — **no
pip dependencies**.
The only optional external is [`ttyd`](https://github.com/tsl0922/ttyd), used by
the `tb web` verbs.

> Want the web dashboard too — every session as an embedded terminal in your
> browser? See **[tmux-browse](https://github.com/itsmygithubacct/tmux-browse)**,
> which builds on this CLI and pulls it in as a submodule.

## Install

Clone and symlink:

```bash
git clone https://github.com/itsmygithubacct/tmux-cli.git ~/tmux-cli
cd ~/tmux-cli && make install      # symlinks tb.py -> ~/bin/tb
tb ls
```

Or pull just the files (no git) with the standalone updater:

```bash
curl -fsSL https://raw.githubusercontent.com/itsmygithubacct/tmux-cli/main/bin/update_tb.py -o update_tb.py
python3 update_tb.py --dir ~/bin/tmux-cli
```

## Quick start

```bash
tb new work                       # create a detached session
tb type work "make test"          # run a line in it
tb wait work --idle 3             # block until the pane goes quiet
tb capture work -n 200            # read the scrollback
tb exec work --json -- pytest -q  # run a command, get {ok, exit_status, output}
tb ls --json                      # machine-readable session list
```

`tb --help` lists every verb. Full reference: **[docs/tb.md](docs/tb.md)**.

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
