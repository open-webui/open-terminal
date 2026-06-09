# OpenTerminal Local Baseline (No Containers)

This document defines the local, standalone baseline for OpenTerminal as an agentic coding and system-ops harness.

## Current State Found (2026-05-10)

- Source repo: `/home/trotsky/Projects/open-terminal`
- Repo is already git-initialized.
- OpenTerminal is currently running via Docker as container `open-terminal` on host port `8010`.
- OpenWebUI is configured to call it via `TERMINAL_SERVER_CONNECTIONS` at `http://127.0.0.1:8010`.
- Local model endpoints from B70 lab:
  - Gemma endpoint: `http://127.0.0.1:8000/v1`
  - Qwen endpoint: `http://127.0.0.1:8091/v1` (currently marked disabled in `local-model-endpoints.z490.json`)
  - OpenVINO test endpoint (documented in B70 README): `http://127.0.0.1:8090`

## Standalone Role Split

- OpenWebUI role: chat/research/RAG UI.
- OpenTerminal role: execution harness for shell workflows, coding changes, and system operations.
- Integration with OpenWebUI is optional and should remain loose.

## Runtime Requirements (Bare Metal)

- Python `>=3.11` (host currently has 3.12.3)
- `uv` (present) or equivalent venv tooling
- Dependencies from `pyproject.toml`
- User-level `systemd` (`systemctl --user`)

## Baseline Safety Defaults

- Bind host to `127.0.0.1` (local-only)
- Run with explicit `--cwd /home/trotsky/Projects`
- Keep API key in `~/.config/open-terminal/api_key`
- Run as user service (no root service files by default)
- Do not mount `/` into the runtime (container-only behavior removed in this mode)

## OpenTerminal CLI Baseline (Operator UX)

- Chat-first REPL: text input sends chat messages by default.
- Slash-command command mode: enter commands with `/...`.
- Slash discoverability:
  - `/` opens interactive command picker (fzf when installed; numbered fallback)
  - `/menu` opens interactive command picker explicitly
  - `/<prefix>` filters command list
  - `TAB` cycles slash command completions
- Persistent status prompt (default enabled):
  - `open-terminal[<mode>|<model>|<cwd>]>`
- Contract mode (default `strict`):
  - validates strict output-format prompts
  - auto-retries with repair prompt on format failure
  - enforces stronger coding-task shape checks (single code block, required signature/tests in constrained prompts)

## Historical Codex-Gemma Notes (retired from active use)

- Async orchestration jobs (historical codex-gemma surface):
  - `/run <task>`
  - `/jobs`
  - `/job status|wait|result|logs|cancel <job_id>`
- Model-side mitigation policy pack (historical `codex-gemma ask` path):
  - strict task-class detection (`exact_literal`, `exact_one_code_block`, `group_anagrams_contract`, `pass_fail_only`, `valid_json_only`, `bullet_list_only`)
  - bounded auto-repair retries with per-task retry caps
  - single-model lock default `on` (prevents cross-model fallback by default)
  - optional fallback routing to Qwen endpoint on strict failure only when explicitly enabled
  - deterministic trace logging in `~/.local/state/open-terminal/codex-gemma-trace.log`
  - failure taxonomy codes in trace output (for example: `schema.missing_signature`, `format.invalid_json`, `request.endpoint_error`)
- Explicit benchmark profile (historical `codex-gemma benchmark` surface):
  - discoverable benchmark-only command surface
  - can target `gemma` or `qwen2`
  - disables single-model lock for controlled benchmark sessions only
- BA-Midnight bounded readiness loop:
  - `scripts/midnight-probe-loop --repo <midnight-gateway-path> --cycles 3 --sleep-sec 20`
  - optional execute mode: append `--execute` to run fee-bearing deploy probe only after readiness gate opens (`availableCoinCountEnd > 0`)
  - writes run log + summary under `~/.local/state/open-terminal/midnight-probe-loop/`

## Known Limitations

- OpenTerminal itself does not enforce approval prompts for destructive shell commands; that policy must come from the calling agent/workflow.
- `--cwd` sets server process working directory but does not hard sandbox all command paths.
- If OpenWebUI keeps polling a stale terminal URL/key, logs will show repeated auth or connectivity errors until updated.
