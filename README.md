# gemini-delegate

Headless Gemini CLI delegation wrappers for Codex-style workflows.

- `gemini_delegate.py`: single delegated run (`patch`, `review`, `tests`, `alt`)
- `gemini_fanout.py`: run multiple delegate roles in parallel and print consolidated output

## Safety model

- Gemini never edits files directly.
- Patch mode requires Gemini to output a single fenced `diff` block.
- You apply patches yourself (`git apply --check` then `git apply`).

## Quick usage

```bash
echo "TASK: list edge cases" | scripts/gemini_delegate.py --mode review
```

```bash
cat payload.txt | scripts/gemini_delegate.py --mode patch --files path/to/file.py --extract-diff > /tmp/patch.diff
git apply --check /tmp/patch.diff
git apply /tmp/patch.diff
```

```bash
cat payload.txt | scripts/gemini_fanout.py --jobs patch review tests alt --concurrency 3
```

## Retries

Both wrappers support retry controls for transient Gemini capacity/timeouts:

- `--retry-window-sec`
- `--retry-initial-backoff-sec`
- `--retry-max-backoff-sec`

## Requirements

- Python 3.10+
- `gemini` CLI installed and authenticated
