# OpenTerminal Model Baseline

- generated_at: `2026-05-11T15:44:28Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `10/10` (100.0%)
- avg_latency_ms: `5011`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1889` | ok |
| `codeblock_factorial` | `True` | `4875` | ok |
| `group_anagrams_contract` | `True` | `16056` | ok |
| `group_anagrams_contract_repeat` | `True` | `16562` | ok |
| `exact_literal` | `True` | `887` | ok |
| `exact_literal_alt` | `True` | `736` | ok |
| `exact_literal_short` | `True` | `503` | ok |
| `codeblock_sum_list` | `True` | `2768` | ok |
| `codeblock_reverse_words` | `True` | `2276` | ok |
| `codeblock_palindrome` | `True` | `3564` | ok |
