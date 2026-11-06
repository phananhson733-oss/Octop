from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.infra.backup import pg_dump
from octop.infra.errors import OctopError


def test_require_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_dump.shutil, "which", lambda _name: None)
    with pytest.raises(OctopError, match="pg_dump not found"):
        pg_dump._require_tool("pg_dump")


def test_dump_postgres_can_exclude_chat_table_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    called: list[str] = []
    monkeypatch.setattr(pg_dump, "_require_tool", lambda _name: "pg_dump")

    def fake_run(command, **_kwargs):
        called.extend(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(pg_dump.subprocess, "run", fake_run)
    pg_dump.dump_postgres(
        "postgresql://example",
        tmp_path / "backup.dump",
        exclude_table_data=("sessions", "threads"),
    )

    assert called.count("--exclude-table-data") == 2
    assert "sessions" in called
    assert "threads" in called


@pytest.mark.parametrize("restore", [False, True])
def test_pg_client_password_is_not_in_process_arguments(monkeypatch, tmp_path, restore):
    monkeypatch.setattr(pg_dump, "_require_tool", lambda name: name)

    def run(command, **kwargs):
        assert "audit-password" not in str(command)
        assert kwargs["env"]["PGPASSWORD"] == "audit-password"
        if restore:
            assert "--single-transaction" in command and "--exit-on-error" in command
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(pg_dump.subprocess, "run", run)
    operation = pg_dump.restore_postgres if restore else pg_dump.dump_postgres
    operation("postgresql://audit:audit-password@example/audit", tmp_path / "backup.dump")


@pytest.mark.parametrize("code", [1, 2, -9])
def test_restore_nonzero_exit_is_never_success(monkeypatch, tmp_path, code):
    monkeypatch.setattr(pg_dump, "_require_tool", lambda name: name)
    monkeypatch.setattr(
        pg_dump.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=code,
            stderr="restore failed",
            stdout="",
        ),
    )
    with pytest.raises(OctopError):
        pg_dump.restore_postgres("postgresql://example/audit", tmp_path / "backup.dump")
