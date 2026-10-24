"""Regression tests for /files/matches hanging on special files (issue #185).

Reading a device node (e.g. under /dev) blocks forever. A FIFO with no writer
blocks the same way on open(), so it is used here as a portable stand-in.
"""
import os
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from open_terminal import main
from open_terminal.utils import search

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")

HEADERS = {"Authorization": "Bearer test-key"}
TIMEOUT = 15
REQUEST_THREAD = "matches-request"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "notes.txt").write_text("a needle in a haystack\n")
    fifo = tmp_path / "needle.pipe"
    os.mkfifo(fifo)
    yield tmp_path
    # If the endpoint is stuck opening the FIFO (the bug), keep releasing it so the
    # worker thread and the test process can exit instead of hanging CI.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(t.name == REQUEST_THREAD and t.is_alive() for t in threading.enumerate()):
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        time.sleep(0.05)


def get_matches(path, **params):
    outcome = {}

    def request():
        client = TestClient(main.app)
        outcome["response"] = client.get(
            "/files/matches", params={"query": "needle", "path": str(path), **params}, headers=HEADERS
        )

    thread = threading.Thread(target=request, name=REQUEST_THREAD, daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    assert not thread.is_alive(), f"/files/matches did not return within {TIMEOUT}s"
    return outcome["response"]


def check(response):
    assert response.status_code == 200
    results = {item["name"]: item for item in response.json()["results"]}
    assert [m["line"] for m in results["notes.txt"]["content_matches"]] == [1]
    # The FIFO is listed by name but its content is never read.
    assert results["needle.pipe"]["content_matches"] == []


def test_python_fallback_does_not_read_special_files(tree, monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    check(get_matches(tree))


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_ripgrep_path_does_not_read_special_files(tree):
    check(get_matches(tree))


def test_python_fallback_stays_on_one_filesystem(tree, monkeypatch):
    """Directories on another device (like /dev, /proc, /sys) are not searched."""
    other = tree / "other_fs"
    other.mkdir()
    (other / "inside.txt").write_text("needle\n")
    real_device = search._device
    monkeypatch.setattr(search, "_device", lambda p: -1 if os.path.basename(p) == "other_fs" else real_device(p))
    monkeypatch.setattr(search.shutil, "which", lambda name: None)

    response = get_matches(tree)

    assert response.status_code == 200
    paths = [item["relative_path"] for item in response.json()["results"]]
    assert "notes.txt" in paths
    assert not any(p.startswith("other_fs") for p in paths)
