# OpenTerminal Model Baseline

- generated_at: `2026-05-11T13:00:00Z`
- cases: `scripts/model_baseline_cases.json`

## gemma

- endpoint: `http://127.0.0.1:8000/v1`
- status: unreachable
- error: `<urlopen error [Errno 111] Connection refused>`

## qwen2

- endpoint: `http://127.0.0.1:8091/v1`
- model_id: `Qwen3.6-27B-Q4_K_M.gguf`
- compliance: `3/3` (100.0%)
- avg_latency_ms: `10948`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `2458` | ok |
| `group_anagrams_contract` | `True` | `29444` | ok |
| `exact_literal` | `True` | `943` | ok |
