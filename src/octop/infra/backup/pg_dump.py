"""PostgreSQL dump/restore via client tools on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from octop.infra.errors import ErrorCode, OctopError


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            f"{name} not found on PATH; required for PostgreSQL backup/restore",
        )
    return path


def _client_connection(conninfo: str) -> tuple[str, dict[str, str]]:
    """Keep the database password out of process arguments and error commands."""
    params = {
        key: str(value) for key, value in conninfo_to_dict(conninfo).items() if value is not None
    }
    env = dict(os.environ)
    password = params.pop("password", None)
    if password is not None:
        env["PGPASSWORD"] = password
    return make_conninfo(**params), env


def dump_postgres(
    conninfo: str,
    dest: Path,
    *,
    exclude_table_data: Sequence[str] = (),
) -> None:
    pg_dump = _require_tool("pg_dump")
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe_conninfo, env = _client_connection(conninfo)
    cmd = [pg_dump, "-Fc", "-f", str(dest), "--dbname", safe_conninfo]
    for table in exclude_table_data:
        cmd.extend(["--exclude-table-data", table])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            f"pg_dump failed: {proc.stderr.strip() or proc.stdout.strip()}",
        )


def restore_postgres(conninfo: str, dump_file: Path) -> None:
    pg_restore = _require_tool("pg_restore")
    safe_conninfo, env = _client_connection(conninfo)
    proc = subprocess.run(
        [
            pg_restore,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
            "--dbname",
            safe_conninfo,
            str(dump_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            f"pg_restore failed: {proc.stderr.strip() or proc.stdout.strip()}",
        )
