#!/usr/bin/env python3
"""Standalone puller for the ``tb`` CLI — fetches tb.py (and the lib/
package it needs) from a tmux-cli GitHub release, no git required.

This script depends on nothing but the Python standard library, so you can
copy it anywhere — or curl it straight down — and use it to drop a working
``tb`` into a directory that isn't a git clone:

    curl -fsSL https://raw.githubusercontent.com/itsmygithubacct/tmux-cli/main/bin/update_tb.py -o update_tb.py
    python3 update_tb.py --dir ~/bin/tmux-cli

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
from pathlib import Path, PurePosixPath

DEFAULT_REPO = "itsmygithubacct/tmux-cli"
_UA = {"User-Agent": "tmux-cli-update_tb"}
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


class _NoLibError(Exception):
    """Raised when the archive contains no ``lib/`` members."""


def _extract_lib(tf: tarfile.TarFile, root: str, dest_lib: Path) -> None:
    """Extract ``<root>/lib/`` from ``tf`` over ``dest_lib`` (refresh, no prune).

    Copy regular files explicitly instead of using ``TarFile.extractall``.
    This keeps the traversal and link protections available on every supported
    Python version (3.10+); tarfile's ``filter="data"`` API only arrived in
    Python 3.12. The completed temporary tree is copied into place only after
    every archive member has passed validation.
    """
    root_path = PurePosixPath(root)
    if root_path.is_absolute() or len(root_path.parts) != 1 \
            or root_path.parts[0] in {".", ".."}:
        raise tarfile.TarError(f"unsafe archive root: {root!r}")

    prefix = root_path.parts + ("lib",)
    members: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    found_lib = False
    for member in tf.getmembers():
        path = PurePosixPath(member.name)
        parts = path.parts
        if len(parts) < len(prefix) or parts[:len(prefix)] != prefix:
            continue
        found_lib = True
        if path.is_absolute() or ".." in parts:
            raise tarfile.TarError(f"unsafe archive member: {member.name!r}")
        if not (member.isdir() or member.isfile()):
            raise tarfile.TarError(
                f"links and special files are not allowed: {member.name!r}")
        relative = parts[len(prefix):]
        if relative:
            members.append((member, relative))

    if not found_lib or not any(member.isfile() for member, _ in members):
        raise _NoLibError(root)

    with tempfile.TemporaryDirectory() as td:
        src_lib = Path(td) / "lib"
        src_lib.mkdir()
        written: set[tuple[str, ...]] = set()
        for member, relative in members:
            if relative in written:
                raise tarfile.TarError(
                    f"duplicate archive member: {member.name!r}")
            written.add(relative)
            target = src_lib.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise tarfile.TarError(
                    f"could not read archive member: {member.name!r}")
            with source, target.open("xb") as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o644)
        shutil.copytree(src_lib, dest_lib, dirs_exist_ok=True)


def _path_present(path: Path) -> bool:
    """Like ``Path.exists()``, but also true for a broken symlink."""
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    """Remove a file/symlink/tree during rollback."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _install_staged(staged_tb: Path, staged_lib: Path | None,
                    dest: Path) -> None:
    """Replace the installed files, restoring the old version on failure.

    Every rename stays on ``dest``'s filesystem. Old paths live inside the
    unique staging directory until both new paths are installed, so an I/O
    failure cannot leave a new ``tb.py`` paired with an old ``lib/`` (or the
    inverse).
    """
    local_tb = dest / "tb.py"
    local_lib = dest / "lib"
    backup_dir = staged_tb.parent / "backup"
    backup_dir.mkdir()
    backup_tb = backup_dir / "tb.py"
    backup_lib = backup_dir / "lib"

    moved_tb = False
    moved_lib = False
    installed_tb = False
    installed_lib = False
    try:
        if _path_present(local_tb):
            local_tb.replace(backup_tb)
            moved_tb = True
        if staged_lib is not None and _path_present(local_lib):
            local_lib.replace(backup_lib)
            moved_lib = True

        if staged_lib is not None:
            staged_lib.replace(local_lib)
            installed_lib = True
        staged_tb.replace(local_tb)
        installed_tb = True
    except OSError as install_error:
        rollback_errors: list[str] = []
        for installed, path in (
            (installed_tb, local_tb),
            (installed_lib, local_lib),
        ):
            if installed:
                try:
                    _remove_path(path)
                except OSError as e:
                    rollback_errors.append(f"remove {path}: {e}")
        for moved, backup, original in (
            (moved_lib, backup_lib, local_lib),
            (moved_tb, backup_tb, local_tb),
        ):
            if moved:
                try:
                    backup.replace(original)
                except OSError as e:
                    rollback_errors.append(f"restore {original}: {e}")
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise OSError(
                f"{install_error}; rollback also failed: {detail}",
            ) from install_error
        raise


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

    local_ver = None
    local_lib = dest / "lib" / "version.py"
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
            _extract_member_text(tf, root, "lib/version.py") or "")
        _say(f"available: {pulled_ver or '?'}")

        if args.check:
            same = local_ver and pulled_ver and local_ver == pulled_ver
            _say("up to date" if same else
                 f"update available: {local_ver or '(none)'} -> {pulled_ver or '?'}")
            return 0

        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".update-tb-", dir=dest) as td:
            stage = Path(td)
            staged_tb = stage / "tb.py"
            try:
                staged_tb.write_text(tb_text, encoding="utf-8")
                staged_tb.chmod(0o755)
            except OSError as e:
                return _die(f"could not stage tb.py: {e}")

            staged_lib: Path | None = None
            if not args.file_only:
                staged_lib = stage / "lib"
                try:
                    _extract_lib(tf, root, staged_lib)
                except _NoLibError:
                    return _die(f"lib/ not found in {args.repo}@{ref}")
                except (tarfile.TarError, OSError) as e:
                    return _die(f"refusing to extract lib/: {e}")

            try:
                _install_staged(staged_tb, staged_lib, dest)
            except OSError as e:
                return _die(f"could not install update: {e}")

        wrote = ["tb.py"]
        if not args.file_only:
            wrote.append("lib/")

    _say(f"wrote {', '.join(wrote)} ({pulled_ver or ref}) to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
