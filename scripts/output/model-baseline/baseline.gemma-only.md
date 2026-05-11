# OpenTerminal Model Baseline

- generated_at: `2026-05-11T13:02:16Z`
- cases: `scripts/model_baseline_cases.json`

## gemma

- endpoint: `http://127.0.0.1:8000/v1`
- model_id: `gemma-4-26B-A4B-it.Q4_K_H.gguf`
- compliance: `3/3` (100.0%)
- avg_latency_ms: `3206`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `975` | ok |
| `group_anagrams_contract` | `True` | `8313` | ok |
| `exact_literal` | `True` | `332` | ok |
