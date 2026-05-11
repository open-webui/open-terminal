# OpenTerminal Model Baseline

- generated_at: `2026-05-11T15:52:39Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `10/10` (100.0%)
- avg_latency_ms: `2743`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1148` | ok |
| `codeblock_factorial` | `True` | `2699` | ok |
| `group_anagrams_contract` | `True` | `7842` | ok |
| `group_anagrams_contract_repeat` | `True` | `9351` | ok |
| `exact_literal` | `True` | `537` | ok |
| `exact_literal_alt` | `True` | `592` | ok |
| `exact_literal_short` | `True` | `427` | ok |
| `codeblock_sum_list` | `True` | `1610` | ok |
| `codeblock_reverse_words` | `True` | `1321` | ok |
| `codeblock_palindrome` | `True` | `1907` | ok |

