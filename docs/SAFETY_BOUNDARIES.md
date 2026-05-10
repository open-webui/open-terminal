# Safety Boundaries (Baseline)

These are operational boundaries for this harness baseline:

1. Default working area is `/home/trotsky/Projects`.
2. Avoid destructive commands unless explicitly approved for the current task.
3. Do not edit `/etc`, `/usr`, `/var`, or service unit files without confirmation.
4. Prefer `git diff` before and after significant edits.
5. Log important operational changes in commit messages and `journalctl` output.

## Optional Local Guard Helper

Use `scripts/guarded-run.sh` for manual command execution with basic path/risk checks.

Example:

```bash
scripts/guarded-run.sh "git status"
scripts/guarded-run.sh "rm -rf /tmp/test" --force
```
