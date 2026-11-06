"""Run agent shell commands as an unprivileged user.

Octop runs as root in its container, so a shell command could read the server's
``/proc/<pid>/environ`` (database URL, API tokens) or rewrite Octop's own files.
With ``OCTOP_SHELL_RUN_AS=uid:gid`` the local shell backend's ``subprocess.run`` gets
``user`` / ``group`` / ``extra_groups=[]``: the child drops to that user before it
execs ``/bin/sh``, so nothing in the command's environment (``PATH``, ``LD_PRELOAD``
from a workspace ``.env``) ever runs as root. Subagents share the backend, so they
are covered too.

The user owns nothing outside what it is handed. Before each command, for the agent
workspace (only one under ``$OCTOP_HOME/agents/``) and each ``OCTOP_SHELL_WRITABLE_DIRS``:

* the root becomes ``root:gid`` mode ``1775``: the shell can add files but, with the
  sticky bit, cannot rename or replace the root itself or the root-owned entries in it.
  In the workspace those are what Octop reads back into the prompt: dot entries
  (``.octop/``, ``.env``) and upper-case ``*.md`` files (``SOUL.md`` and their backups);
* everything else in the root is chowned to the user, with what is in it, because
  Octop keeps writing root-owned files there (uploads, files sent from the dashboard).

The walks go through directory fds and never follow links.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from octop.infra.utils.paths import PathLayout
from octop.infra.utils.write_roots import writable_dirs_from_env

logger = logging.getLogger(__name__)

_RUN_AS_ENV = "OCTOP_SHELL_RUN_AS"
# HOME stays the server's (/data) in the allowed env; the shell user cannot write there.
_SHELL_HOME = "/tmp"
_ROOT_MODE = 0o1775
# Workspace entries Octop reads back into the prompt: SOUL.md, AGENTS.md, SOUL.md.bak, ...
_PROMPT_FILE = re.compile(r"[A-Z][A-Z0-9_-]*\.md(\..+)?")


def _workspace_keeps(name: str) -> bool:
    return name.startswith(".") or _PROMPT_FILE.fullmatch(name) is not None


def shell_run_as_from_env() -> tuple[int, int] | None:
    raw = os.environ.get(_RUN_AS_ENV, "").strip()
    if not raw:
        return None
    uid, _, gid = raw.partition(":")
    return int(uid), int(gid or uid)


def _chown_entry(name: str, dirfd: int, uid: int, gid: int) -> None:
    try:
        entry = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(entry.st_mode) or (entry.st_uid, entry.st_gid) == (uid, gid):
        return
    # A second hard link may be a file outside the tree (an Octop file).
    if not stat.S_ISDIR(entry.st_mode) and entry.st_nlink > 1:
        return
    os.chown(name, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def hand_over_tree(root: Path, *, uid: int, gid: int) -> None:
    """Chown *root* and everything under it to ``uid:gid``, skipping links."""
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        return
    if (st.st_uid, st.st_gid) != (uid, gid):
        os.chown(root, uid, gid, follow_symlinks=False)
    for _dirpath, dirnames, filenames, dirfd in os.fwalk(root, follow_symlinks=False):
        for name in (*dirnames, *filenames):
            _chown_entry(name, dirfd, uid, gid)


def _keep_nothing(_name: str) -> bool:
    return False


def hand_over_root(
    root: Path, *, uid: int, gid: int, keep: Callable[[str], bool] = _keep_nothing
) -> None:
    """Open *root* to ``uid:gid`` as the module docstring describes; *keep* names the
    entries that stay as they are."""
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        return
    if (st.st_uid, st.st_gid) != (0, gid):
        os.chown(root, 0, gid, follow_symlinks=False)
    if stat.S_IMODE(st.st_mode) != _ROOT_MODE:
        os.chmod(root, _ROOT_MODE, follow_symlinks=False)
    dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for entry in os.scandir(dirfd):
            if keep(entry.name):
                continue
            if entry.is_dir(follow_symlinks=False):
                hand_over_tree(root / entry.name, uid=uid, gid=gid)
            else:
                _chown_entry(entry.name, dirfd, uid, gid)
    finally:
        os.close(dirfd)


def hand_over_workspace(workspace: Path, *, uid: int, gid: int) -> None:
    hand_over_root(workspace, uid=uid, gid=gid, keep=_workspace_keeps)


def agent_workspace_of(cwd: Any) -> Path | None:
    """The agent workspace *cwd* is in, or ``None`` outside ``$OCTOP_HOME/agents/``.

    The local shell falls back to ``/`` when the workspace is missing; that must never
    be handed over.
    """
    if not cwd:
        return None
    agents = PathLayout.from_env().agents_dir.resolve()
    try:
        rel = Path(cwd).resolve().relative_to(agents)
    except (OSError, ValueError):
        return None
    return agents / rel.parts[0] if rel.parts else None


def _hand_over(cwd: Any, *, uid: int, gid: int) -> None:
    workspace = agent_workspace_of(cwd)
    jobs: list[tuple[Path, Callable[[str], bool]]] = (
        [(workspace, _workspace_keeps)] if workspace else []
    )
    jobs += [(d, _keep_nothing) for d in writable_dirs_from_env()]
    for path, keep in jobs:
        try:
            hand_over_root(path, uid=uid, gid=gid, keep=keep)
        except FileNotFoundError:
            continue
        except OSError:
            # The command still runs; it only fails where it needs to write.
            logger.warning("ShellUser could not hand over %s", path, exc_info=True)


class _RunAsSubprocess:
    """``subprocess`` with ``run`` dropping to ``uid:gid`` in the child."""

    def __init__(self, uid: int, gid: int) -> None:
        self._uid = uid
        self._gid = gid

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        _hand_over(kwargs.get("cwd"), uid=self._uid, gid=self._gid)
        env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
        env["HOME"] = _SHELL_HOME
        kwargs.update(env=env, user=self._uid, group=self._gid, extra_groups=[])
        return subprocess.run(*args, **kwargs)


def install_shell_user(uid: int, gid: int) -> None:
    """Point the local shell backends' ``subprocess`` at :class:`_RunAsSubprocess`."""
    from deepagents.backends import local_shell as deepagents_shell
    from octop_harness.backends import local_shell as harness_shell

    proxy = _RunAsSubprocess(uid, gid)
    for module in (harness_shell, deepagents_shell):
        setattr(module, "subprocess", proxy)  # noqa: B010


__all__ = [
    "agent_workspace_of",
    "hand_over_root",
    "hand_over_tree",
    "hand_over_workspace",
    "install_shell_user",
    "shell_run_as_from_env",
]
