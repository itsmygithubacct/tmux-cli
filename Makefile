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
	@mkdir -p "$(PREFIX)"
	@ln -sf "$(CURDIR)/tb.py" "$(PREFIX)/tb"
	@install -d -m 0700 "$(TMUX_CLI_HOME)"
	@install -m 0600 docs/logging.md "$(TMUX_CLI_HOME)/logging.md"
	@echo "linked $(PREFIX)/tb -> $(CURDIR)/tb.py"
	@echo "installed $(TMUX_CLI_HOME)/logging.md"

uninstall:
	@rm -f $(PREFIX)/tb
	@echo "removed $(PREFIX)/tb"

version:
	@$(PY) tb.py --version
