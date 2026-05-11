# OpenTerminal Model Baseline

- generated_at: `2026-05-11T13:27:42Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `10/10` (100.0%)
- avg_latency_ms: `2640`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1065` | ok |
| `codeblock_factorial` | `True` | `2611` | ok |
| `group_anagrams_contract` | `True` | `8355` | ok |
| `group_anagrams_contract_repeat` | `True` | `8516` | ok |
| `exact_literal` | `True` | `442` | ok |
| `exact_literal_alt` | `True` | `469` | ok |
| `exact_literal_short` | `True` | `366` | ok |
| `codeblock_sum_list` | `True` | `1483` | ok |
| `codeblock_reverse_words` | `True` | `1238` | ok |
| `codeblock_palindrome` | `True` | `1864` | ok |
