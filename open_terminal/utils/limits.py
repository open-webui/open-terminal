"""Byte budgets for model-facing tool responses."""

import json

from open_terminal.env import MAX_TOOL_OUTPUT_SIZE

# Everything splitlines() treats as a line break, which is how line numbers
# have always been counted here.
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def json_bytes(payload: object) -> int:
    """Size of *payload* serialized the way the API returns it."""
    return len(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


def escaped_bytes(text: str) -> int:
    """Size of *text* once JSON-escaped, as it lands in a response body."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def truncate_escaped(text: str, max_bytes: int) -> str:
    """Cut *text* so that its JSON-escaped size fits *max_bytes*."""
    low, high = 0, min(len(text), max_bytes)
    while low < high:
        middle = (low + high + 1) // 2
        if escaped_bytes(text[:middle]) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def excerpt_around(text: str, index: int, max_bytes: int) -> tuple[str, bool]:
    """Bounded window of *text* around the match at character *index*."""
    if len(text) <= max_bytes and len(text.encode("utf-8")) <= max_bytes:
        return text, False
    # A character is never fewer than one byte, so this window holds every
    # byte the excerpt could need without encoding the whole line.
    window_start = max(0, index - max_bytes)
    window = text[window_start : index + max_bytes].encode("utf-8")
    match_start = len(text[window_start:index].encode("utf-8"))
    start = min(max(0, match_start - max_bytes // 4), len(window) - max_bytes)
    return window[start : start + max_bytes].decode("utf-8", errors="ignore"), True


class LineBudget:
    """Collects lines until the tool output budget is spent.

    Lines are charged at their JSON-escaped size, and *reserved* is the room
    the rest of the response needs, so the budget covers the whole body. A
    line that exceeds the budget on its own is cut, so a file consisting of
    one very long line still returns something useful.
    """

    def __init__(self, reserved: int = 0):
        # A response whose own fields eat most of the limit still returns a
        # useful chunk, rather than a character at a time forever.
        self.limit = max(MAX_TOOL_OUTPUT_SIZE - reserved, MAX_TOOL_OUTPUT_SIZE // 4)
        self.lines: list[str] = []
        self.used = 0
        self.returned_lines = 0
        self.truncated = False
        self.line_truncated = False

    def add(self, line: str) -> bool:
        """Append *line*. Returns ``False`` once the budget is spent."""
        if self.truncated:
            return False
        size = escaped_bytes(line)
        if self.used + size > self.limit:
            self.truncated = True
            if not self.lines:
                text = truncate_escaped(line, self.limit)
                self.lines.append(text)
                self.returned_lines = 1
                self.line_truncated = True
            return False
        self.lines.append(line)
        self.used += size
        self.returned_lines += 1
        return True

    def clip(self, text: str) -> tuple[str, int]:
        """Cut *text* to what could still be returned, and by how much.

        One character past the limit, so a line too long to return whole stays
        over budget and is reported as cut rather than looking complete.
        """
        keep_chars = self.limit + 1
        if len(text) <= keep_chars:
            return text, 0
        return text[:keep_chars], len(text) - keep_chars

    def result(self) -> dict:
        """The collected lines and how the budget cut them short."""
        return {
            "content": "".join(self.lines),
            "returned_lines": self.returned_lines,
            "truncated": self.truncated,
            "line_truncated": self.line_truncated,
        }


def fit_lines(lines: list[str], start: int, end: int, line_offset: int, reserved: int) -> dict:
    """Collect an already split line range under the byte budget."""
    budget = LineBudget(reserved)
    line_length = 0
    for position, line in enumerate(lines[start:end]):
        if not budget.add(line[line_offset:] if position == 0 else line):
            if budget.line_truncated:
                line_length = len(line.rstrip(LINE_BREAKS))
            break
    return {**budget.result(), "total_lines": len(lines), "line_length": line_length}


def fit_items(payload: dict, key: str, *, keep_last: bool = False) -> int:
    """Drop items from ``payload[key]`` until *payload* fits the byte budget.

    Keeps the first items, or the last ones when *keep_last* is set (process
    output, where the newest entries matter). One item that exceeds the budget
    on its own is kept, so a cursor always advances. Returns the number dropped.
    """
    items = payload[key]
    payload[key] = []
    used = json_bytes(payload)
    kept = []
    for item in reversed(items) if keep_last else items:
        used += json_bytes(item) + (1 if kept else 0)
        if used > MAX_TOOL_OUTPUT_SIZE and kept:
            break
        kept.append(item)
    payload[key] = kept[::-1] if keep_last else kept
    return len(items) - len(kept)
