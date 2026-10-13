"""Thin wrappers around the git commands the orchestrator needs."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def toplevel(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel").strip())


def head(repo: Path) -> str:
    try:
        return _git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        raise GitError("repository has no commits; create an initial commit first") from exc


def exclude_path(repo: Path) -> Path:
    return repo / _git(repo, "rev-parse", "--git-path", "info/exclude").strip()


def untracked(repo: Path) -> list[str]:
    out = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p]


def changed_files(repo: Path, base: str) -> list[str]:
    tracked = [p for p in _git(repo, "diff", "--name-only", base, "--").splitlines() if p]
    return sorted(set(tracked) | set(untracked(repo)))


def diff(repo: Path, base: str) -> str:
    """Working tree vs. base commit, including untracked (non-ignored) files."""
    parts = [_git(repo, "diff", base, "--")]
    for path in untracked(repo):
        # --no-index exits 1 when files differ, which is always the case here.
        parts.append(_git(repo, "diff", "--no-index", "--", "/dev/null", path, ok_codes=(0, 1)))
    return "".join(parts)


def fingerprint(repo: Path) -> str:
    """Hash of everything a read-only agent must not change."""
    h = hashlib.sha256()
    h.update(_git(repo, "rev-parse", "HEAD").encode())
    h.update(_git(repo, "status", "--porcelain=v1", "--untracked-files=all").encode())
    h.update(diff(repo, "HEAD").encode())
    return h.hexdigest()
