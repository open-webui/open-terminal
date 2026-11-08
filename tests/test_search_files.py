"""Regression tests for GitHub issue #208:
The @ menu's Filesystem list shows deeply nested folders instead of the top level.

These tests directly exercise the production open_terminal.main.search_files()
asynchronous endpoint and its run_io execution path.
"""
import asyncio
import os
import sys
import types
from unittest.mock import MagicMock

# open_terminal.main requires OPEN_TERMINAL_API_KEY to be set at import time
os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")

import open_terminal.main as main_mod


class DummyStat:
    """Mock file stat metadata for controlled candidate generation."""

    def __init__(self, size=1024, mtime=1700000000.0):
        self.st_size = size
        self.st_mtime = mtime


def run_search(
    monkeypatch,
    target,
    entries,
    query="",
    type_filter="any",
    limit=20,
    stats=None,
    path_mod=None,
    sep=None,
):
    """Executes the production search_files() endpoint with controlled entries and stat."""
    if stats is None:
        stats = {}
    if path_mod is None:
        path_mod = os.path
    if sep is None:
        sep = os.sep

    orig_stat = os.stat

    def mock_stat(path, *args, **kwargs):
        if path in stats:
            return stats[path]
        try:
            return orig_stat(path, *args, **kwargs)
        except OSError:
            return DummyStat()

    def mock_search_entries(fs, root, show_hidden, *, git_files_as_files=False):
        for path, kind in entries:
            yield path, kind

    os_proxy = types.SimpleNamespace(
        path=path_mod,
        sep=sep,
        stat=mock_stat,
    )

    monkeypatch.setattr(main_mod, "search_entries", mock_search_entries)
    monkeypatch.setattr(main_mod, "os", os_proxy)

    class MockFS:
        def resolve_path(self, p, cwd=None):
            return target

        def is_path_allowed(self, p):
            return True

    req = MagicMock()
    req.headers = {}

    async def _invoke():
        return await main_mod.search_files(
            req,
            query=query,
            path=target,
            limit=limit,
            type=type_filter,
            show_hidden=False,
            fs=MockFS(),
        )

    return asyncio.run(_invoke())["results"]


def test_empty_query_ranks_top_level_first_issue_208(monkeypatch, tmp_path):
    """Reproduces issue #208 against the production search_files() implementation:

    Deeply nested subfolders with short names must NOT rank ahead of root-level
    folders with longer names.
    """
    target = str(tmp_path)
    entries = [
        (f"{target}/minecraft-server", "directory"),
        (f"{target}/projects", "directory"),
        (f"{target}/documents", "directory"),
        (f"{target}/minecraft-server/data", "directory"),
        (f"{target}/minecraft-server/data/bluemap", "directory"),
        (f"{target}/minecraft-server/data/bluemap/web", "directory"),
        (f"{target}/minecraft-server/data/bluemap/web/dist", "directory"),
        (f"{target}/minecraft-server/data/bluemap/web/dist/js", "directory"),
        (f"{target}/minecraft-server/data/bluemap/web/dist/js/3", "directory"),
        (f"{target}/minecraft-server/data/bluemap/web/dist/js/3/a", "directory"),
    ]

    results = run_search(monkeypatch, target, entries, query="", type_filter="directory", limit=5)
    result_names = [r["name"] for r in results]

    assert result_names[0] == "documents"
    assert result_names[1] == "minecraft-server"
    assert result_names[2] == "projects"
    assert "a" not in result_names[:3]
    assert "3" not in result_names[:3]


def test_empty_query_alphabetical_order(monkeypatch, tmp_path):
    """Entries at the same depth must be sorted alphabetically when query is empty,

    not scrambled by basename length.
    """
    target = str(tmp_path)
    entries = [
        (f"{target}/docs", "directory"),          # len 4
        (f"{target}/assets", "directory"),        # len 6
        (f"{target}/open_terminal", "directory"), # len 13
        (f"{target}/bin", "directory"),           # len 3
    ]

    results = run_search(monkeypatch, target, entries, query="", type_filter="directory", limit=10)
    result_names = [r["name"] for r in results]

    assert result_names == ["assets", "bin", "docs", "open_terminal"]


def test_non_empty_query_relevance_ranking(monkeypatch, tmp_path):
    """Exact match (rank 0) > Prefix match (rank 1) > Substring match (rank 2)."""
    target = str(tmp_path)
    entries = [
        (f"{target}/my_config_backup.py", "file"),  # substring match for 'config' -> rank 2
        (f"{target}/config_utils.py", "file"),      # prefix match for 'config' -> rank 1
        (f"{target}/config", "file"),               # exact match for 'config' -> rank 0
    ]

    results = run_search(monkeypatch, target, entries, query="config", type_filter="file", limit=10)
    result_names = [r["name"] for r in results]

    assert result_names == ["config", "config_utils.py", "my_config_backup.py"]


def test_non_empty_query_depth_within_same_rank(monkeypatch, tmp_path):
    """When match quality (rank) is identical, shallower paths must be prioritized."""
    target = str(tmp_path)
    entries = [
        (f"{target}/sub/deep/config.py", "file"),  # depth 2, prefix match
        (f"{target}/config.py", "file"),           # depth 0, prefix match
        (f"{target}/sub/config.py", "file"),       # depth 1, prefix match
    ]

    results = run_search(monkeypatch, target, entries, query="config", type_filter="file", limit=10)
    result_paths = [r["path"].replace("\\", "/") for r in results]
    expected_paths = [
        f"{target}/config.py".replace("\\", "/"),
        f"{target}/sub/config.py".replace("\\", "/"),
        f"{target}/sub/deep/config.py".replace("\\", "/"),
    ]

    assert result_paths == expected_paths


def test_exact_match_beats_shallow_substring_match(monkeypatch, tmp_path):
    """An exact match in a subdirectory must outrank a shallow substring match."""
    target = str(tmp_path)
    entries = [
        (f"{target}/something_main_test.py", "file"),  # depth 0, substring match -> rank 2
        (f"{target}/core/main", "file"),               # depth 1, exact match -> rank 0
    ]

    results = run_search(monkeypatch, target, entries, query="main", type_filter="file", limit=10)
    result_names = [r["name"] for r in results]

    assert result_names == ["main", "something_main_test.py"]


def test_cross_platform_path_separators(monkeypatch, tmp_path):
    """Ensures backslash (Windows) and forward-slash (POSIX) paths compute depth identically."""
    target = str(tmp_path)

    # Forward-slash (POSIX style) paths
    target_posix = target.replace("\\", "/")
    entries_posix = [
        (f"{target_posix}/sub/file.py", "file"),
        (f"{target_posix}/file.py", "file"),
    ]
    results_posix = run_search(monkeypatch, target_posix, entries_posix, query="")
    assert [r["name"] for r in results_posix] == ["file.py", "file.py"]
    assert results_posix[0]["path"] == f"{target_posix}/file.py"

    # Backslash (Windows style) paths
    if sys.platform == "win32":
        target_win = target.replace("/", "\\")
        entries_win = [
            (f"{target_win}\\sub\\file.py", "file"),
            (f"{target_win}\\file.py", "file"),
        ]
        results_win = run_search(monkeypatch, target_win, entries_win, query="")
        assert [r["name"] for r in results_win] == ["file.py", "file.py"]
        assert results_win[0]["path"] == f"{target_win}\\file.py"


def test_type_filtering(monkeypatch, tmp_path):
    """Type filtering ('file', 'directory', 'any') behaves as expected."""
    target = str(tmp_path)
    entries = [
        (f"{target}/docs", "directory"),
        (f"{target}/README.md", "file"),
    ]

    dir_results = run_search(monkeypatch, target, entries, query="", type_filter="directory")
    assert len(dir_results) == 1
    assert dir_results[0]["name"] == "docs"

    file_results = run_search(monkeypatch, target, entries, query="", type_filter="file")
    assert len(file_results) == 1
    assert file_results[0]["name"] == "README.md"

    any_results = run_search(monkeypatch, target, entries, query="", type_filter="any")
    assert len(any_results) == 2


def test_payload_structure_preserved(monkeypatch, tmp_path):
    """Ensures all expected API response dictionary keys are preserved from production search_files()."""
    target = str(tmp_path)
    test_file = f"{target}/test.txt"
    stat = DummyStat(size=2048, mtime=1701234567.8)
    entries = [(test_file, "file")]

    results = run_search(monkeypatch, target, entries, query="", stats={test_file: stat})
    assert len(results) == 1
    item = results[0]

    assert set(item.keys()) == {"path", "name", "type", "size", "modified"}
    assert item["path"] == test_file
    assert item["name"] == "test.txt"
    assert item["type"] == "file"
    assert item["size"] == 2048
    assert item["modified"] == 1701234567.8


def test_whitespace_query_handled_as_empty(monkeypatch, tmp_path):
    """Whitespace-only query string should be treated as empty."""
    target = str(tmp_path)
    entries = [
        (f"{target}/docs", "directory"),
        (f"{target}/assets", "directory"),
    ]

    results = run_search(monkeypatch, target, entries, query="   ", type_filter="directory")
    assert [r["name"] for r in results] == ["assets", "docs"]


def test_posix_filename_with_literal_backslash_depth(monkeypatch, tmp_path):
    """Ensures a POSIX filename containing a literal backslash is not counted

    as an additional directory level.
    """
    import posixpath

    target = str(tmp_path).replace("\\", "/")
    # A root-level file whose name contains literal backslashes
    root_backslash_file = f"{target}/file\\with\\backslashes.txt"
    # A file nested one level down in a subdirectory
    nested_file = f"{target}/sub/nested.txt"

    entries = [
        (nested_file, "file"),
        (root_backslash_file, "file"),
    ]

    results = run_search(
        monkeypatch,
        target,
        entries,
        query="",
        type_filter="file",
        limit=10,
        path_mod=posixpath,
        sep="/",
    )
    result_names = [r["name"] for r in results]

    assert result_names == ["file\\with\\backslashes.txt", "nested.txt"]
    assert results[0]["path"] == root_backslash_file
