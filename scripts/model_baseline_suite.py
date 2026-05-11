#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINTS = {
    "gemma": "http://127.0.0.1:8000/v1",
    "qwen2": "http://127.0.0.1:8091/v1",
}


@dataclass
class CaseResult:
    case_id: str
    ok: bool
    latency_ms: int
    detail: str
    output_preview: str


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 90) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Authorization": "Bearer dummy"}
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw)


def resolve_model_id(endpoint: str) -> str:
    data = http_json(endpoint.rstrip("/") + "/models", None, timeout=8)
    items = data.get("data") or []
    if items and isinstance(items, list) and isinstance(items[0], dict):
        return str(items[0].get("id") or "default")
    return "default"


def extract_text(resp: dict[str, Any]) -> str:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    if isinstance(msg, list):
        parts = []
        for item in msg:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join([p for p in parts if p]).strip()
    return str(msg).strip()


def check_exact_one_code_block(text: str) -> tuple[bool, str]:
    blocks = re.findall(r"```(?:python)?\n[\s\S]*?\n```", text)
    if len(blocks) != 1:
        return False, f"expected exactly one fenced code block, found {len(blocks)}"
    cleaned = re.sub(r"```(?:python)?\n[\s\S]*?\n```", "", text).strip()
    if cleaned:
        return False, "expected no text outside the code block"
    return True, "ok"


def check_group_anagrams_contract(text: str) -> tuple[bool, str]:
    blocks = re.findall(r"```(?:python)?\n([\s\S]*?)\n```", text)
    if len(blocks) != 1:
        return False, "expected exactly one fenced code block"
    code = blocks[0]
    if "def group_anagrams(words: list[str]) -> list[list[str]]" not in code:
        return False, "missing exact function signature"
    if '"""' not in code and "'''" not in code:
        return False, "missing docstring"
    assert_count = len(re.findall(r"^\s*assert\s+", code, flags=re.MULTILINE))
    if assert_count != 5:
        return False, f"expected exactly 5 asserts, found {assert_count}"
    return True, "ok"


def check_exact_literal(text: str, literal: str) -> tuple[bool, str]:
    if text.strip() != literal:
        return False, f"expected exact literal {literal!r}"
    return True, "ok"


def run_case(endpoint: str, model: str, case: dict[str, Any]) -> CaseResult:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": case["prompt"]}],
        "temperature": 0,
    }
    t0 = time.time()
    resp = http_json(endpoint.rstrip("/") + "/chat/completions", payload, timeout=180)
    latency_ms = int((time.time() - t0) * 1000)
    text = extract_text(resp)

    check = case["check"]
    if check == "exact_one_code_block":
        ok, detail = check_exact_one_code_block(text)
    elif check == "group_anagrams_contract":
        ok, detail = check_group_anagrams_contract(text)
    elif check == "exact_literal":
        ok, detail = check_exact_literal(text, str(case["literal"]))
    else:
        ok, detail = False, f"unknown check: {check}"

    preview = text[:180].replace("\n", "\\n")
    return CaseResult(case_id=case["id"], ok=ok, latency_ms=latency_ms, detail=detail, output_preview=preview)


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cases file must be a JSON array")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Run baseline prompt-contract suite against local model endpoints.")
    parser.add_argument("--cases", default="scripts/model_baseline_cases.json", help="Path to JSON test cases.")
    parser.add_argument("--out-json", default="scripts/output/model-baseline/baseline.latest.json", help="Output JSON path.")
    parser.add_argument("--out-md", default="scripts/output/model-baseline/baseline.latest.md", help="Output markdown path.")
    parser.add_argument(
        "--models",
        default="gemma,qwen2",
        help="Comma-separated model keys from built-in endpoint map: gemma,qwen2",
    )
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    selected = [m.strip() for m in args.models.split(",") if m.strip()]

    report: dict[str, Any] = {
        "report_type": "open-terminal-model-baseline",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases_path": str(Path(args.cases)),
        "models": {},
    }

    md_lines = [
        "# OpenTerminal Model Baseline",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- cases: `{args.cases}`",
        "",
    ]

    for key in selected:
        endpoint = DEFAULT_ENDPOINTS.get(key)
        if not endpoint:
            report["models"][key] = {"status": "unknown-model-key"}
            md_lines.extend([f"## {key}", "", "- status: unknown model key", ""])
            continue
        try:
            model_id = resolve_model_id(endpoint)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            report["models"][key] = {"status": "unreachable", "endpoint": endpoint, "error": str(exc)}
            md_lines.extend([f"## {key}", "", f"- endpoint: `{endpoint}`", "- status: unreachable", f"- error: `{exc}`", ""])
            continue

        results: list[dict[str, Any]] = []
        passed = 0
        for case in cases:
            try:
                r = run_case(endpoint, model_id, case)
            except Exception as exc:  # noqa: BLE001
                r = CaseResult(case_id=case["id"], ok=False, latency_ms=0, detail=f"request-failed: {exc}", output_preview="")
            passed += 1 if r.ok else 0
            results.append(
                {
                    "case_id": r.case_id,
                    "ok": r.ok,
                    "latency_ms": r.latency_ms,
                    "detail": r.detail,
                    "output_preview": r.output_preview,
                }
            )

        total = len(results)
        compliance = round((passed / total) * 100, 1) if total else 0.0
        avg_latency = int(sum(x["latency_ms"] for x in results) / total) if total else 0
        report["models"][key] = {
            "status": "ok",
            "endpoint": endpoint,
            "model_id": model_id,
            "summary": {
                "passed": passed,
                "total": total,
                "compliance_percent": compliance,
                "avg_latency_ms": avg_latency,
            },
            "results": results,
        }

        md_lines.extend(
            [
                f"## {key}",
                "",
                f"- endpoint: `{endpoint}`",
                f"- model_id: `{model_id}`",
                f"- compliance: `{passed}/{total}` ({compliance}%)",
                f"- avg_latency_ms: `{avg_latency}`",
                "",
                "| case | ok | latency_ms | detail |",
                "|---|---:|---:|---|",
            ]
        )
        for item in results:
            md_lines.append(f"| `{item['case_id']}` | `{item['ok']}` | `{item['latency_ms']}` | {item['detail']} |")
        md_lines.append("")

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out_md.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
