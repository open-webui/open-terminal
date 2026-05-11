# OpenTerminal Model Baseline

- generated_at: `2026-05-11T13:01:21Z`
- cases: `scripts/model_baseline_cases.json`

## qwen2

- endpoint: `http://127.0.0.1:8091/v1`
- model_id: `Qwen3.6-27B-Q4_K_M.gguf`
- compliance: `3/3` (100.0%)
- avg_latency_ms: `10952`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `3136` | ok |
| `group_anagrams_contract` | `True` | `28779` | ok |
| `exact_literal` | `True` | `941` | ok |
