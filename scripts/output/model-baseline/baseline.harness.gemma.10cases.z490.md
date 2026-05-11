# OpenTerminal Model Baseline

- generated_at: `2026-05-11T15:52:12Z`
- cases: `scripts/model_baseline_cases.json`
- mode: `harness`

## gemma

- endpoint: `via-codex-gemma`
- model_id: `codex-gemma-policy-path`
- compliance: `10/10` (100.0%)
- avg_latency_ms: `2632`

| case | ok | latency_ms | detail |
|---|---:|---:|---|
| `codeblock_only` | `True` | `1083` | ok |
| `codeblock_factorial` | `True` | `2751` | ok |
| `group_anagrams_contract` | `True` | `8109` | ok |
| `group_anagrams_contract_repeat` | `True` | `8116` | ok |
| `exact_literal` | `True` | `420` | ok |
| `exact_literal_alt` | `True` | `450` | ok |
| `exact_literal_short` | `True` | `359` | ok |
| `codeblock_sum_list` | `True` | `1407` | ok |
| `codeblock_reverse_words` | `True` | `1869` | ok |
| `codeblock_palindrome` | `True` | `1765` | ok |

