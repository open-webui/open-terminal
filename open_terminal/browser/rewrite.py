"""Browser resource rewriting, adapted from Computer's browser proxy."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

_URL_ATTRIBUTES = {"href", "src", "action", "poster", "data", "formaction"}
_SKIP_SCHEMES = ("data:", "javascript:", "mailto:", "tel:", "about:", "blob:")


def proxy_path(prefix: str, url: str) -> str:
    """Return the same-origin URL used by the Browser iframe for an upstream URL."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only HTTP(S) URLs can be opened in Browser")
    host = quote(parsed.netloc, safe=":[]")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{prefix}/frame/{parsed.scheme}/{host}{path}{query}{fragment}"


def target_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only HTTP(S) URLs can be opened in Browser")
    parsed.port  # Validate malformed port numbers before dispatching a request.
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def rewrite_url(value: str, base_url: str, prefix: str) -> str:
    value = value.strip()
    if not value or value.startswith("#") or value.lower().startswith(_SKIP_SCHEMES):
        return value
    try:
        url = urljoin(base_url, value)
        return proxy_path(prefix, url)
    except ValueError:
        return value


def _rewrite_srcset(value: str, base_url: str, prefix: str) -> str:
    parts = []
    for item in value.split(","):
        if not item.strip():
            continue
        url, *descriptor = item.strip().split(maxsplit=1)
        parts.append(" ".join([rewrite_url(url, base_url, prefix), *descriptor]))
    return ", ".join(parts)


class _HtmlRewriter(HTMLParser):
    def __init__(self, base_url: str, prefix: str) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.prefix = prefix
        self.output: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, close=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, close=False)

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], close: bool) -> None:
        rewritten: list[tuple[str, str | None]] = []
        for key, value in attrs:
            lower = key.lower()
            if lower == "integrity":
                continue
            if value and lower in _URL_ATTRIBUTES:
                value = rewrite_url(value, self.base_url, self.prefix)
            elif value and lower == "srcset":
                value = _rewrite_srcset(value, self.base_url, self.prefix)
            elif tag.lower() == "meta" and lower == "content":
                value = re.sub(
                    r"(?i)(url\s*=\s*)([^;]+)",
                    lambda match: (
                        match.group(1)
                        + rewrite_url(
                            match.group(2).strip(), self.base_url, self.prefix
                        )
                    ),
                    value,
                )
            rewritten.append((key, value))
        attrs_text = "".join(
            f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"'
            for key, value in rewritten
        )
        self.output.append(f"<{tag}{attrs_text}{' /' if close else ''}>")

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")


def rewrite_html(content: str, base_url: str, prefix: str, final_url: str) -> str:
    parser = _HtmlRewriter(base_url, prefix)
    parser.feed(content)
    parser.close()
    config = json.dumps({"prefix": prefix, "url": final_url}).replace("<", "\\u003c")
    runtime = (
        f"<script>window.__terminalBrowser={config};</script>"
        f'<script src="{html.escape(prefix, quote=True)}/runtime.js"></script>'
    )
    rendered = "".join(parser.output)
    if urlsplit(final_url).hostname in {"localhost", "127.0.0.1", "::1"}:
        rendered = rewrite_javascript(rendered, final_url, prefix)
    if re.search(r"<head(?:\s[^>]*)?>", rendered, re.I):
        return re.sub(
            r"(<head(?:\s[^>]*)?>)", r"\1" + runtime, rendered, count=1, flags=re.I
        )
    return runtime + rendered


def rewrite_css(content: str, base_url: str, prefix: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        quote, value = match.group(1), match.group(2)
        return f"url({quote}{rewrite_url(value, base_url, prefix)}{quote})"

    content = re.sub(
        r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", replace_url, content, flags=re.I
    )
    return re.sub(
        r"(@import\s+(?:url\()?['\"])([^'\"]+)",
        lambda match: match.group(1) + rewrite_url(match.group(2), base_url, prefix),
        content,
        flags=re.I,
    )


def rewrite_javascript(content: str, base_url: str, prefix: str) -> str:
    """Rewrite root-relative ES module imports used by local dev servers."""
    # Vite builds hot-update imports dynamically from `base`, outside import declarations.
    if urlsplit(base_url).path.endswith("/@vite/client"):
        content = re.sub(
            r'(\bconst base\s*=\s*)(["\'])(/[^"\']*)\2',
            lambda match: match[1]
            + json.dumps(proxy_path(prefix, urljoin(base_url, match[3]))),
            content,
        )
    return re.sub(
        r"((?:from\s*|import\s*\(\s*|import\s*)['\"])(/[^'\"]+)",
        lambda match: match.group(1) + rewrite_url(match.group(2), base_url, prefix),
        content,
    )
