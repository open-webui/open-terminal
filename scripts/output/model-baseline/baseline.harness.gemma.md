# OpenTerminal Model Baseline

- generated_at: `2026-05-11T13:21:59Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `3/3` (100.0%)
- avg_latency_ms: `3693`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1062` | ok |
| `group_anagrams_contract` | `True` | `9561` | ok |
| `exact_literal` | `True` | `457` | ok |
