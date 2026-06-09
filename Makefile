# tmux-cli — the `tb` tmux CLI for humans and LLMs.
PY ?= python3
PREFIX ?= $(HOME)/bin

.PHONY: help test install uninstall version

help:
	@echo "make test       run the unit test suite"
	@echo "make install    symlink tb.py -> $(PREFIX)/tb"
	@echo "make uninstall  remove the $(PREFIX)/tb symlink"
	@echo "make version    print the tb version"

test:
	$(PY) -m unittest discover tests

install:
	@mkdir -p $(PREFIX)
	@ln -sf $(CURDIR)/tb.py $(PREFIX)/tb
	@echo "linked $(PREFIX)/tb -> $(CURDIR)/tb.py"

uninstall:
	@rm -f $(PREFIX)/tb
	@echo "removed $(PREFIX)/tb"

version:
	@$(PY) tb.py --version
