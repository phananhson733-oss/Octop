"""Shell user switch, path rules guard and deployment path denies (customer lockdown)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.prebuilt.tool_node import ToolCallRequest
from octop_harness.security.models import SecurityPolicy

from octop.infra.agents.middleware import shell_user
from octop.infra.agents.middleware.path_rules_guard import PathRulesGuardMiddleware
from octop.infra.agents.security.policy_store import with_deployment_path_denies

posix_only = pytest.mark.skipif(os.name != "posix", reason="chown, links and fd walks are POSIX")


def _request(name: str, args: dict[str, object]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


def test_run_as_parses_uid_and_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_SHELL_RUN_AS", raising=False)
    assert shell_user.shell_run_as_from_env() is None
    monkeypatch.setenv("OCTOP_SHELL_RUN_AS", "10001:10002")
    assert shell_user.shell_run_as_from_env() == (10001, 10002)
    monkeypatch.setenv("OCTOP_SHELL_RUN_AS", "10001")
    assert shell_user.shell_run_as_from_env() == (10001, 10001)


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[Any]]:
    runs: list[dict[str, Any]] = []
    handed: list[Any] = []
    monkeypatch.setattr(shell_user.subprocess, "run", lambda *_a, **kw: runs.append(kw))
    monkeypatch.setattr(shell_user, "_hand_over", lambda cwd, **_k: handed.append(cwd))
    return runs, handed


def test_run_drops_to_the_user_in_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs, handed = _capture_run(monkeypatch)
    proxy = shell_user._RunAsSubprocess(10001, 10002)

    proxy.run("ls", shell=True, cwd=str(tmp_path), env={"PATH": "/x", "LD_PRELOAD": "y"})

    kwargs = runs[0]
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (10001, 10002, [])
    assert kwargs["env"] == {"PATH": "/x", "LD_PRELOAD": "y", "HOME": "/tmp"}
    assert handed == [str(tmp_path)]
    assert proxy.DEVNULL is shell_user.subprocess.DEVNULL


def test_run_keeps_an_empty_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, _handed = _capture_run(monkeypatch)
    shell_user._RunAsSubprocess(1, 1).run("ls", env={})
    assert runs[0]["env"] == {"HOME": "/tmp"}


def test_install_points_both_shell_backends_at_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepagents.backends import local_shell as deepagents_shell
    from octop_harness.backends import local_shell as harness_shell

    for module in (harness_shell, deepagents_shell):
        monkeypatch.setattr(module, "subprocess", module.subprocess)

    shell_user.install_shell_user(10001, 10001)

    assert isinstance(harness_shell.subprocess, shell_user._RunAsSubprocess)
    assert isinstance(deepagents_shell.subprocess, shell_user._RunAsSubprocess)


@posix_only
def test_hand_over_tree_skips_links_and_owned_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "clips"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "clip.mp4").write_bytes(b"x")
    outside = tmp_path / "octop.json"
    outside.write_text("{}")
    (root / "link").symlink_to(outside)
    os.link(outside, root / "hard")
    calls: list[str] = []
    monkeypatch.setattr(shell_user.os, "chown", lambda path, *_a, **_k: calls.append(str(path)))

    shell_user.hand_over_tree(root, uid=os.getuid() + 1, gid=os.getgid())

    assert sorted(Path(c).name for c in calls) == ["clip.mp4", "clips", "sub"]
    calls.clear()
    shell_user.hand_over_tree(root, uid=os.getuid(), gid=os.getgid())
    assert calls == []


def _record_owner_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int]]]:
    chowns: list[tuple[str, int, int]] = []
    chmods: list[tuple[str, int]] = []
    monkeypatch.setattr(
        shell_user.os, "chown", lambda p, u, g, **_k: chowns.append((Path(p).name, u, g))
    )
    monkeypatch.setattr(shell_user.os, "chmod", lambda p, m, **_k: chmods.append((str(p), m)))
    return chowns, chmods


@posix_only
def test_hand_over_workspace_keeps_prompt_and_system_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    (ws / ".octop" / "sessions").mkdir(parents=True)
    (ws / "inbound").mkdir()
    (ws / "inbound" / "upload.mp4").write_bytes(b"x")
    for name in ("SOUL.md", "SOUL.md.bak", ".env", "sent.mp4"):
        (ws / name).write_text("x")
    chowns, chmods = _record_owner_changes(monkeypatch)

    shell_user.hand_over_workspace(ws, uid=10001, gid=10002)

    assert ("ws", 0, 10002) in chowns
    assert chmods == [(str(ws), 0o1775)]
    names = {name for name, _u, _g in chowns}
    assert {"inbound", "upload.mp4", "sent.mp4"} <= names
    assert not names & {".octop", "sessions", "SOUL.md", "SOUL.md.bak", ".env"}


@posix_only
def test_writable_dir_root_stays_root_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "clip-work"
    (root / "job").mkdir(parents=True)
    chowns, chmods = _record_owner_changes(monkeypatch)

    shell_user.hand_over_root(root, uid=10001, gid=10002)

    assert chowns == [("clip-work", 0, 10002), ("job", 10001, 10002)]
    assert chmods == [(str(root), 0o1775)]


def test_only_agent_workspaces_are_handed_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    agents = (tmp_path / "agents").resolve()
    (agents / "A1" / "成片").mkdir(parents=True)

    assert shell_user.agent_workspace_of(agents / "A1" / "成片") == agents / "A1"
    assert shell_user.agent_workspace_of(agents / "A1") == agents / "A1"
    assert shell_user.agent_workspace_of(agents) is None
    assert shell_user.agent_workspace_of(tmp_path) is None
    assert shell_user.agent_workspace_of(Path(tmp_path.anchor)) is None
    assert shell_user.agent_workspace_of(None) is None


def _denied_policy(monkeypatch: pytest.MonkeyPatch) -> SecurityPolicy:
    monkeypatch.setenv("OCTOP_FS_DENY", os.pathsep.join(["/", "/proc", "/proc/**"]))
    return with_deployment_path_denies(SecurityPolicy.defaults())


def test_deployment_denies_go_first_and_turn_the_guard_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = _denied_policy(monkeypatch).filesystem.rules
    assert rules[0].paths == ["/", "/proc", "/proc/**"]
    assert rules[0].operations == ["read", "write"]
    assert rules[1].paths == SecurityPolicy.defaults().filesystem.rules[0].paths

    off = SecurityPolicy.from_dict(
        {**SecurityPolicy.defaults().to_dict(), "filesystem": {"enabled": False}}
    )
    turned_on = with_deployment_path_denies(off)
    assert turned_on.filesystem.enabled is True
    assert len(turned_on.filesystem.rules) == 1


def test_no_deployment_denies_leaves_policy_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_FS_DENY", raising=False)
    policy = SecurityPolicy.defaults()
    assert with_deployment_path_denies(policy) is policy


async def _guarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    args: dict[str, object],
    *,
    confine: bool = True,
) -> bool:
    """Return True when the call was refused."""
    import tempfile

    temp_root = tmp_path / "temp"
    temp_root.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    monkeypatch.setenv("OCTOP_SHELL_WRITABLE_DIRS", str(tmp_path / "clip-work"))
    permissions = _denied_policy(monkeypatch).resolve_permissions()
    mw = PathRulesGuardMiddleware(
        permissions, workspace_dir=tmp_path / "ws", confine_writes=confine
    )
    handler = AsyncMock(return_value="ran")
    result = await mw.awrap_tool_call(_request(name, args), handler)
    if result == "ran":
        return False
    assert getattr(result, "status", None) == "error"
    handler.assert_not_awaited()
    return True


def _posix(*values: object) -> Any:
    return pytest.param(*values, marks=posix_only)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "blocked"),
    [
        _posix("send_file_to_user", {"file_path": "/proc/1/environ"}, True),
        _posix("send_file_to_user", {"file_path": "/proc/self/environ"}, True),
        _posix("send_file_to_user", {"file_path": "/etc/passwd"}, True),
        _posix("send_file_to_user", {"file_path": "/srv/../proc/1/environ"}, True),
        _posix("send_file_to_user", {"file_path": "成片/a.mp4"}, False),
        _posix("send_file_to_user", {"file_path": " /proc/self/environ"}, True),
        ("send_file_to_user", {"file_path": "././~/.octop/config.json"}, True),
        _posix("send_file_to_user", {"file_path": "/opt/app/README.md"}, True),
        _posix("grep", {"pattern": "DATABASE_URL"}, True),
        _posix("ls", {}, True),
        _posix("ls", {"path": "/data/.octop/agents/A1"}, False),
        ("read_file", {"file_path": "/proc/1/environ"}, False),  # harness FilesystemGuard's job
        ("task", {"description": "x"}, True),
        ("write_file", {"file_path": "/opt/app/.hidden/x.py", "content": ""}, True),
        ("write_file", {"file_path": "../x.txt", "content": ""}, True),
        # File tools resolve relative paths from the backend root ``/``, not the workspace.
        ("edit_file", {"file_path": "notes/a.md"}, True),
        ("delete", {"file_path": "/usr/lib/x"}, True),
        ("write_file", {"file_path": "~/x.txt", "content": ""}, True),
    ],
)
async def test_path_rules_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    args: dict[str, object],
    blocked: bool,
) -> None:
    assert await _guarded(monkeypatch, tmp_path, name, args) is blocked


@pytest.mark.asyncio
async def test_guard_allows_workspace_temp_and_clip_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for target in (
        tmp_path / "ws" / "成片" / "a.mp4",
        tmp_path / "temp" / "a",
        tmp_path / "clip-work" / "a",
    ):
        for tool in ("write_file", "send_file_to_user"):
            assert not await _guarded(monkeypatch, tmp_path, tool, {"file_path": str(target)})
    # A root itself cannot be deleted or replaced.
    for root in (tmp_path / "ws", tmp_path / "clip-work"):
        assert await _guarded(monkeypatch, tmp_path, "delete", {"file_path": str(root)})


@pytest.mark.asyncio
async def test_writes_and_task_open_without_confinement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert not await _guarded(monkeypatch, tmp_path, "task", {}, confine=False)
    assert not await _guarded(
        monkeypatch, tmp_path, "write_file", {"file_path": "/opt/x.py"}, confine=False
    )


@posix_only
@pytest.mark.asyncio
async def test_guard_follows_a_linked_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "ws" / "out").symlink_to(tmp_path / "elsewhere")
    args = {"file_path": str(tmp_path / "ws" / "out" / "new.py")}
    assert await _guarded(monkeypatch, tmp_path, "write_file", args)


@posix_only
@pytest.mark.asyncio
async def test_guard_checks_the_path_with_its_trailing_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "temp").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "temp" / "target ").symlink_to(tmp_path / "elsewhere" / "x.py")
    args = {"file_path": str(tmp_path / "temp" / "target ")}
    assert await _guarded(monkeypatch, tmp_path, "write_file", args)
