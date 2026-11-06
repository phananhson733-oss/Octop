from __future__ import annotations

import os
from pathlib import Path

import pytest

from octop.infra.utils.write_roots import backend_host_paths, path_shape_ok, write_target_allowed

posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks")


def test_new_file_under_root_allowed(tmp_path: Path) -> None:
    assert write_target_allowed(tmp_path / "a" / "b.txt", [tmp_path])


def test_root_itself_only_with_allow_root(tmp_path: Path) -> None:
    assert not write_target_allowed(tmp_path, [tmp_path])
    assert write_target_allowed(tmp_path, [tmp_path], allow_root=True)


def test_dotdot_escape_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert not write_target_allowed(root / ".." / "x.txt", [root])


@posix_only
def test_linked_parent_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "out").symlink_to(other)
    assert not write_target_allowed(root / "out" / "new.py", [root])


def test_backend_host_paths_cover_every_normalization_pass() -> None:
    home = os.path.expanduser("~")
    config = os.path.join(home, ".octop/config.json")
    assert backend_host_paths(" /proc/1/environ ") == ["/proc/1/environ"]
    assert config in backend_host_paths("./~/.octop/config.json")
    assert config in backend_host_paths("././~/.octop/config.json")
    assert "/etc/hosts" in backend_host_paths("./ /etc/hosts")
    assert os.path.join(home, "x") in backend_host_paths("~/x")
    for relative in ("./notes/a.md", "notes/a.md", "成片/a b.mp4", "."):
        assert backend_host_paths(relative) == []


def test_path_shape_ok() -> None:
    for ok in (
        ".",
        "needle.txt",
        "成片/a b.mp4",
        "/var/tmp/short-drama/job/out.mp4",
        ".media-cache/x.png",
    ):
        assert path_shape_ok(ok), ok
    for bad in (
        "./~/x",
        "././~/x",
        "./ /etc/hosts",
        " /proc/1/environ",
        "a\\b",
        "a/../b",
        "~/x",
        "a/ b",
    ):
        assert not path_shape_ok(bad), bad
