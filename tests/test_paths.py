# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the path sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.safety.paths import check_path


def test_inside_root(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert check_path(tmp_path / "a" / "x.mp4", [tmp_path]) == (tmp_path / "a" / "x.mp4").resolve()


def test_outside_root_lists_roots(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(DavinciError) as ei:
        check_path(tmp_path / "other", [root])
    assert ei.value.code == ErrorCode.PATH_NOT_ALLOWED
    assert str(root.resolve()) in ei.value.details["allowed_roots"]


def test_dotdot_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(DavinciError):
        check_path(root / ".." / "x", [root])


def test_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(DavinciError) as ei:
        check_path(root / "link" / "f.txt", [root])
    assert ei.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_null_byte(tmp_path: Path) -> None:
    with pytest.raises(DavinciError) as ei:
        check_path(str(tmp_path) + "/a\0b", [tmp_path])
    assert ei.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_must_exist(tmp_path: Path) -> None:
    with pytest.raises(DavinciError) as ei:
        check_path(tmp_path / "nope", [tmp_path], must_exist=True)
    assert ei.value.code == ErrorCode.NOT_FOUND
    (tmp_path / "yes").write_text("x")
    assert check_path(tmp_path / "yes", [tmp_path], must_exist=True).exists()
