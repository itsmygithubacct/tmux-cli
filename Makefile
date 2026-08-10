# tmux-cli — the `tb` tmux CLI for humans and LLMs.
PY ?= python3
PREFIX ?= $(HOME)/bin
TMUX_CLI_HOME ?= $(HOME)/.gpu_terminal/tmux-cli

.PHONY: help test ci clean install uninstall version

help:
	@echo "make test       run the unit test suite"
	@echo "make ci         run the same checks as continuous integration"
	@echo "make clean      remove generated Python caches"
	@echo "make install    link tb and install the permanent-log manual"
	@echo "make uninstall  remove the $(PREFIX)/tb symlink"
	@echo "make version    print the tb version"

test:
	$(PY) -m unittest discover tests

ci: test

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete

install:
	@test ! -L "$(TMUX_CLI_HOME)" || { echo "refusing symlink data path: $(TMUX_CLI_HOME)" >&2; exit 1; }
	@set -eu; \
	bin="$(PREFIX)"; target="$(CURDIR)/tb.py"; link="$$bin/tb"; \
	[ ! -L "$$bin" ] || { echo "refusing symlink install directory: $$bin" >&2; exit 1; }; \
	mkdir -p "$$bin"; \
	[ -d "$$bin" ] || { echo "install path is not a directory: $$bin" >&2; exit 1; }; \
	if [ -e "$$link" ] || [ -L "$$link" ]; then \
		[ -L "$$link" ] || { echo "refusing to replace non-symlink: $$link" >&2; exit 1; }; \
		[ "$$(readlink -- "$$link")" = "$$target" ] \
			|| { echo "refusing to replace unmanaged symlink: $$link" >&2; exit 1; }; \
	fi; \
	tmpdir="$$(mktemp -d "$$bin/.tb-install.XXXXXX")"; \
	trap 'rm -f -- "$$tmpdir/tb"; rmdir -- "$$tmpdir" 2>/dev/null || true' EXIT; \
	ln -s -- "$$target" "$$tmpdir/tb"; \
	$(PY) -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
		"$$tmpdir/tb" "$$link"; \
	rmdir -- "$$tmpdir"; \
	trap - EXIT
	@install -d -m 0700 "$(TMUX_CLI_HOME)"
	@install -m 0600 docs/logging.md "$(TMUX_CLI_HOME)/logging.md"
	@echo "linked $(PREFIX)/tb -> $(CURDIR)/tb.py"
	@echo "installed $(TMUX_CLI_HOME)/logging.md"

uninstall:
	@set -eu; \
	target="$(CURDIR)/tb.py"; link="$(PREFIX)/tb"; \
	if [ -L "$$link" ] && [ "$$(readlink -- "$$link")" = "$$target" ]; then \
		rm -f -- "$$link"; \
		echo "removed $$link"; \
	elif [ -e "$$link" ] || [ -L "$$link" ]; then \
		echo "leaving unmanaged path untouched: $$link" >&2; \
	else \
		echo "already absent: $$link"; \
	fi

version:
	@$(PY) tb.py --version
