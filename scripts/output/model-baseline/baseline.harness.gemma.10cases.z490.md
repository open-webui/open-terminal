# OpenTerminal Model Baseline

- generated_at: `2026-05-11T15:44:28Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `10/10` (100.0%)
- avg_latency_ms: `4840`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1045` | ok |
| `codeblock_factorial` | `True` | `3320` | ok |
| `group_anagrams_contract` | `True` | `10732` | ok |
| `group_anagrams_contract_repeat` | `True` | `15986` | ok |
| `exact_literal` | `True` | `8909` | ok |
| `exact_literal_alt` | `True` | `705` | ok |
| `exact_literal_short` | `True` | `619` | ok |
| `codeblock_sum_list` | `True` | `1626` | ok |
| `codeblock_reverse_words` | `True` | `2537` | ok |
| `codeblock_palindrome` | `True` | `2925` | ok |
