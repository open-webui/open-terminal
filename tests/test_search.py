import itertools
import os
import shutil
import subprocess

import pytest

from open_terminal.utils import search


class AllowAll:
    def is_path_allowed(self, path):
        return True


def names(root, **kwargs):
    return sorted(os.path.relpath(p, root) for p, _ in search.walk_entries(AllowAll(), str(root), **kwargs))


def test_is_regular_file(tmp_path):
    regular = tmp_path / "file.txt"
    regular.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(regular)

    assert search.is_regular_file(str(regular))
    assert search.is_regular_file(str(link))  # symlinks to files resolve; callers skip links separately
    assert not search.is_regular_file(str(tmp_path))
    assert not search.is_regular_file(str(tmp_path / "missing"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")
def test_is_regular_file_rejects_fifo(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert not search.is_regular_file(str(fifo))


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="requires /dev/null")
def test_is_regular_file_rejects_device_node():
    assert not search.is_regular_file("/dev/null")


def test_walk_entries_lists_everything_on_one_device(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("x")
    (tmp_path / ".hidden").write_text("x")

    assert names(tmp_path) == [".hidden", "a", os.path.join("a", "b.txt")]
    assert names(tmp_path, show_hidden=False) == ["a", os.path.join("a", "b.txt")]


def test_walk_entries_skips_directories_on_other_devices(tmp_path, monkeypatch):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "f.txt").write_text("x")
    (tmp_path / "mnt").mkdir()
    (tmp_path / "mnt" / "g.txt").write_text("x")
    real_device = search._device
    monkeypatch.setattr(search, "_device", lambda p: -1 if os.path.basename(p) == "mnt" else real_device(p))

    assert names(tmp_path) == ["local", os.path.join("local", "f.txt")]


@pytest.mark.skipif(not os.path.isdir("/proc") or os.stat("/proc").st_dev == os.stat("/").st_dev,
                    reason="/proc is not a separate filesystem")
def test_walk_entries_from_root_skips_pseudo_filesystems():
    top_level = itertools.takewhile(lambda e: os.path.dirname(e[0]) == "/", search.walk_entries(AllowAll(), "/"))
    top_names = {os.path.basename(path) for path, _ in top_level}
    assert "etc" in top_names or "usr" in top_names
    assert "proc" not in top_names
    if os.stat("/dev").st_dev != os.stat("/").st_dev:
        assert "dev" not in top_names


def test_matching_files_passes_one_file_system_to_ripgrep(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(search.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(search.subprocess, "run", fake_run)

    with search.matching_files("needle", str(tmp_path), show_hidden=False) as matches:
        assert matches("anything") is False

    assert "--one-file-system" in calls[0]
    assert calls[0][-2:] == ["needle", str(tmp_path)]


def test_matching_files_excludes_pseudo_mounts_when_searching_root(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(search.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(search.subprocess, "run", fake_run)

    with search.matching_files("needle", "/", show_hidden=False) as matches:
        assert matches("anything") is False

    for name in ("dev", "proc", "sys"):
        assert ["--glob", f"!{name}/**"] in [calls[0][i:i + 2] for i in range(len(calls[0]) - 1)]


@pytest.mark.skipif(not os.path.isdir("/dev"), reason="requires /dev")
def test_matching_files_uses_safe_fallback_for_pseudo_filesystem_root(monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(search.subprocess, "run", lambda *args, **kwargs: pytest.fail("ripgrep must not run on /dev"))

    with search.matching_files("needle", "/dev", show_hidden=False) as matches:
        assert matches is None


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_matching_files_finds_files_with_ripgrep(tmp_path, monkeypatch):
    hit, miss = tmp_path / "hit.txt", tmp_path / "miss.txt"
    hit.write_text("a Needle here")
    miss.write_text("nothing")

    with search.matching_files("needle", str(tmp_path), show_hidden=False) as matches:
        assert matches(str(hit))
        assert not matches(str(miss))
