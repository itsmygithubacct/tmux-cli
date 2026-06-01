#!/usr/bin/env python3
"""Standalone puller for the ``tb`` CLI — fetches tb.py (and the lib/
package it needs) from a tmux-browse GitHub release, no git required.

Unlike ``bin/update.sh`` (which advances a full git checkout in place),
this script depends on nothing but the Python standard library, so you can
copy it anywhere — or curl it straight down — and use it to drop a working
``tb`` into a directory that isn't a git clone:

    curl -fsSL https://raw.githubusercontent.com/itsmygithubacct/tmux-browse/main/bin/update_tb.py -o update_tb.py
    python3 update_tb.py --dir ~/bin/tmux-browse

``tb.py`` imports the ``lib`` package, so by default this pulls both
``tb.py`` and ``lib/`` (a runnable CLI). Pass ``--file-only`` if you only
want the single ``tb.py`` file (e.g. you already have a matching ``lib/``).

Usage:
    python3 update_tb.py                 # pull latest release into the cwd
    python3 update_tb.py --dir PATH      # ...into PATH instead
    python3 update_tb.py --check         # report local vs latest, write nothing
    python3 update_tb.py --ref v0.7.8.0  # pull a specific tag/branch/sha
    python3 update_tb.py --file-only     # pull just tb.py, not lib/

Exit codes: 0 ok · 1 error · 2 usage.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "itsmygithubacct/tmux-browse"
_UA = {"User-Agent": "tmux-browse-update_tb"}
_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _die(msg: str, code: int = 1) -> "int":
    sys.stderr.write(f"update_tb: {msg}\n")
    return code


def _say(msg: str) -> None:
    print(f"==> {msg}")


def _fetch(url: str, *, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted host)
        return r.read()


def _version_of(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _latest_tag(repo: str) -> str:
    """Newest *published release* tag, via the GitHub API.

    Falls back to the tag list (newest semver-ish core tag) if the repo has
    tags but no published release.
    """
    try:
        raw = _fetch(f"https://api.github.com/repos/{repo}/releases/latest")
        tag = json.loads(raw).get("tag_name")
        if tag:
            return tag
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        pass
    raw = _fetch(f"https://api.github.com/repos/{repo}/tags")
    tags = [t.get("name", "") for t in json.loads(raw)]
    core = [t for t in tags if re.fullmatch(r"v[0-9]+\.[0-9]+(\.[0-9]+){1,2}", t)]
    if not core:
        raise RuntimeError(f"no release or core tag found for {repo}")
    # The tags endpoint is roughly newest-first, but sort defensively.
    core.sort(key=lambda t: [int(p) for p in t[1:].split(".")], reverse=True)
    return core[0]


def _download_tree(repo: str, ref: str) -> tarfile.TarFile:
    """Download the repo tarball for ``ref`` and return an open TarFile."""
    url = f"https://github.com/{repo}/archive/refs/tags/{ref}.tar.gz"
    try:
        blob = _fetch(url)
    except urllib.error.HTTPError:
        # Not a tag (branch / sha) — codeload accepts those under a different path.
        blob = _fetch(f"https://github.com/{repo}/archive/{ref}.tar.gz")
    return tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")


def _extract_member_text(tf: tarfile.TarFile, root: str, rel: str) -> str | None:
    try:
        f = tf.extractfile(f"{root}/{rel}")
    except KeyError:
        return None
    return f.read().decode("utf-8") if f else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="update_tb.py",
        description="Pull tb.py (+ lib/) from a tmux-browse GitHub release.",
    )
    p.add_argument("--dir", default=".",
                   help="destination directory (default: current directory)")
    p.add_argument("--ref",
                   help="git tag/branch/sha to pull (default: latest release)")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help=f"owner/name (default: {DEFAULT_REPO})")
    p.add_argument("--file-only", action="store_true",
                   help="pull only tb.py, not the lib/ package")
    p.add_argument("--check", action="store_true",
                   help="report local vs available version; write nothing")
    args = p.parse_args(argv)

    dest = Path(args.dir).expanduser().resolve()

    try:
        ref = args.ref or _latest_tag(args.repo)
    except Exception as e:  # network / parse / no-tag
        return _die(f"could not determine ref: {e}")

    local_tb = dest / "tb.py"
    local_ver = None
    local_lib = dest / "lib" / "__init__.py"
    if local_lib.is_file():
        local_ver = _version_of(local_lib.read_text(encoding="utf-8"))
    _say(f"repo {args.repo}  ref {ref}")
    _say(f"local: {local_ver or '(none)'}   dest: {dest}")

    try:
        tf = _download_tree(args.repo, ref)
    except Exception as e:
        return _die(f"download failed: {e}")

    with tf:
        # Archives extract under a single top-level dir, e.g. tmux-browse-<sha>/.
        names = tf.getnames()
        if not names:
            return _die("empty archive")
        root = names[0].split("/", 1)[0]

        tb_text = _extract_member_text(tf, root, "tb.py")
        if tb_text is None:
            return _die(f"tb.py not found in {args.repo}@{ref}")
        pulled_ver = _version_of(
            _extract_member_text(tf, root, "lib/__init__.py") or "")
        _say(f"available: {pulled_ver or '?'}")

        if args.check:
            same = local_ver and pulled_ver and local_ver == pulled_ver
            _say("up to date" if same else
                 f"update available: {local_ver or '(none)'} -> {pulled_ver or '?'}")
            return 0

        dest.mkdir(parents=True, exist_ok=True)
        # tb.py — write via temp + replace so a failed write can't truncate it.
        tmp = dest / ".tb.py.partial"
        tmp.write_text(tb_text, encoding="utf-8")
        tmp.replace(local_tb)
        local_tb.chmod(0o755)
        wrote = ["tb.py"]

        if not args.file_only:
            # Extract lib/ to a temp area, then copy over the destination's
            # lib/ (overwriting files; this refreshes rather than prunes).
            with tempfile.TemporaryDirectory() as td:
                members = [m for m in tf.getmembers()
                           if m.name.startswith(f"{root}/lib/")]
                if not members:
                    return _die(f"lib/ not found in {args.repo}@{ref}")
                tf.extractall(td, members=members)  # noqa: S202 (trusted archive)
                src_lib = Path(td) / root / "lib"
                shutil.copytree(src_lib, dest / "lib", dirs_exist_ok=True)
            wrote.append("lib/")

    _say(f"wrote {', '.join(wrote)} ({pulled_ver or ref}) to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
