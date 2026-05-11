# Cross-Host Harness Benchmark (Gemma, 10 Cases)

- generated_at: `2026-05-11`
- suite: `scripts/model_baseline_cases.json`
- mode: `harness` (`codex-gemma ask` strict policy path)

## Summary

- z490 compliance: `10/10` (100.0%)
- nuc compliance: `10/10` (100.0%)
- z490 avg latency: `4840 ms`
- nuc avg latency: `5011 ms`
- delta (nuc - z490): `+171 ms` (about `+3.5%`)

## Interpretation

- Policy correctness is stable across hosts (no compliance drift).
- Latency is slightly higher on NUC route in this run, but close enough that no policy changes are required.
- No validation/fallback/request failure taxonomy events were emitted during this benchmark window.

## Artifacts

- `baseline.harness.gemma.10cases.z490.json`
- `baseline.harness.gemma.10cases.z490.md`
- `baseline.harness.gemma.10cases.nuc.json`
- `baseline.harness.gemma.10cases.nuc.md`
