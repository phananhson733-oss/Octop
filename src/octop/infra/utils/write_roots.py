"""Where Octop may write on the host when it runs as root.

As root, a write to a path the caller chose can land anywhere: Python's
site-packages, Octop's own config, the system. Host-absolute writes from the
dashboard and from agent file tools are kept to the agent workspace, the OS temp
dir and ``OCTOP_SHELL_WRITABLE_DIRS`` (``os.pathsep``-separated).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

WRITABLE_DIRS_ENV = "OCTOP_SHELL_WRITABLE_DIRS"


def runs_as_root() -> bool:
    return os.name == "posix" and os.geteuid() == 0


def writable_dirs_from_env() -> list[Path]:
    raw = os.environ.get(WRITABLE_DIRS_ENV, "")
    return [Path(p.strip()) for p in raw.split(os.pathsep) if p.strip()]


def write_roots(workspace: Path | str) -> list[Path]:
    return [Path(workspace), Path(tempfile.gettempdir()), *writable_dirs_from_env()]


def path_shape_ok(path: str) -> bool:
    """False for a path that needs normalizing before it means anything: outer or
    per-segment whitespace, ``\\``, ``.`` / ``..`` segments, a segment starting with
    ``~``. ``BackendWorkspace`` normalizes those in several passes, so a check on the
    raw text can disagree with the file it opens; refusing them closes that gap.
    ``.`` alone (the workspace root) is fine.
    """
    if path == ".":
        return True
    if path != path.strip() or "\\" in path:
        return False
    return all(
        seg == seg.strip() and seg not in (".", "..") and not seg.startswith("~")
        for seg in path.split("/")
    )


def backend_host_paths(raw: str) -> list[str]:
    """Host paths ``BackendWorkspace`` may open for *raw*; empty when *raw* only ever
    stays workspace-relative.

    Its read path normalizes in several passes (surrounding whitespace dropped, ``\\``
    read as ``/``, a leading ``./`` dropped, leading slashes dropped, then ``~``
    expanded), so every form those steps reach is collected: ``./ /etc/hosts`` and
    ``././~/x`` both end up as host paths.
    """
    seen: set[str] = set()
    hosts: list[str] = []
    todo = [str(raw)]
    while todo:
        s = todo.pop()
        if s in seen:
            continue
        seen.add(s)
        if s.startswith("~"):
            hosts.append(os.path.expanduser(s))
        elif s.startswith("/"):
            hosts.append(s)
        todo.append(s.strip().replace("\\", "/"))
        if s.startswith("./"):
            todo.append(s[2:])
        todo.append(s.lstrip("/"))
    return hosts


def write_target_allowed(
    path: Path | str, roots: Iterable[Path | str], *, allow_root: bool = False
) -> bool:
    """True when *path*, links resolved, lands under one of *roots*.

    ``resolve()`` resolves the part of the path that exists and keeps the rest, so a
    linked parent directory counts even when the file itself is new. A root itself only
    counts with *allow_root* (searching it is fine, deleting it is not). The roots stay
    root-owned (``shell_user``), so the shell user cannot swap one for a link.
    """
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            resolved_root = Path(root).resolve()
            if target.is_relative_to(resolved_root) and (allow_root or target != resolved_root):
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


__all__ = [
    "WRITABLE_DIRS_ENV",
    "runs_as_root",
    "writable_dirs_from_env",
    "write_roots",
    "write_target_allowed",
]
