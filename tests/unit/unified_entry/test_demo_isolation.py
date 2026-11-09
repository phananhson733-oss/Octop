"""Inherited browser/database overrides cannot escape a fresh synthetic demo."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_demo_replaces_external_browser_paths_before_settings_import(tmp_path):
    sentinel = tmp_path / "real-recorder"
    sentinel.mkdir()
    marker = sentinel / "keep.txt"
    marker.write_text("unchanged")
    env = dict(os.environ)
    env.update(
        {
            "OCTOP_DATABASE_URL": "postgresql://invalid.example/not-used",
            "BROWSER_RECORD_REPLAY_DIR": str(sentinel),
            "BROWSER_USE_PROFILES_DIR": str(sentinel),
            "BROWSER_USE_SCREENSHOTS_DIR": str(sentinel),
            "BROWSER_USE_CDP_HOST": "invalid.example",
            "BROWSER_USE_CDP_WS_URL": "ws://invalid.example/not-used",
        }
    )
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "prototypes.unified_entry.demo",
            "--sab-ui",
            "unused",
            "--check-isolation",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"isolated": True}
    assert marker.read_text() == "unchanged"
    assert list(sentinel.iterdir()) == [marker]
