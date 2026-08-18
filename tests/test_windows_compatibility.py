import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")

from open_terminal.main import serve_file
from open_terminal.utils.fs import UserFS


@pytest.mark.asyncio
async def test_serve_file_preserves_windows_drive_path():
    filesystem = object()
    with patch("open_terminal.main.view_file", new_callable=AsyncMock) as view_file:
        await serve_file("C:/Users/test/index.html", filesystem)

    view_file.assert_awaited_once_with(path="C:/Users/test/index.html", fs=filesystem)


def test_is_writable_falls_back_when_effective_ids_are_unsupported():
    filesystem = UserFS(home="C:/Users/test")
    path = "C:/Users/test"

    with patch.object(os, "access", side_effect=[NotImplementedError, True]) as access:
        assert filesystem._is_writable_sync(path) is True

    assert access.call_count == 2
    assert access.call_args_list[1].kwargs == {}
