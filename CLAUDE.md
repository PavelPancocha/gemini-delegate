# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Gemini delegate

### When to use

- Hard/ambiguous tasks needing a second opinion.
- Patch proposal generation.
- Test-plan drafting.

### How to build payload

- Include `TASK` and explicit constraints.
- Include relevant file paths.
- Include only minimal snippets needed, or use `--files` to inline files automatically.

### Safety boundary

- Gemini MUST NOT directly modify files.
- Gemini proposes diffs and analysis only; caller applies changes and validates with tests.

### Patch workflow

1. Generate patch: `gemini-delegate --mode patch --extract-diff`.
2. Apply patch safely: `git apply --check` then `git apply`.
3. Run tests.
4. Feed failures back into another delegate round.

## What this project does

`gemini-delegate` is a thin Python wrapper around the `gemini` CLI that enforces a **patch-safe delegation boundary**: Gemini proposes unified diffs; the caller applies them. It has two scripts:

- `scripts/gemini_delegate.py` — single-mode delegate runner (`patch`, `review`, `tests`, `alt`)
- `scripts/gemini_fanout.py` — async fan-out that runs multiple modes in parallel via `gemini-delegate`

The scripts are installed as `~/.local/bin/gemini-delegate` and `~/.local/bin/gemini-fanout` (symlinks to `~/.agents/skills/gemini-delegate/scripts/`).

## Running the scripts

Both scripts read from stdin and write to stdout. No build step required.

```bash
# Single review pass
echo "TASK: ..." | scripts/gemini_delegate.py --mode review

# Patch proposal → extract diff → apply
cat payload.txt | scripts/gemini_delegate.py --mode patch --files src/foo.py --extract-diff > /tmp/patch.diff
git apply --check /tmp/patch.diff && git apply /tmp/patch.diff

# Parallel fan-out
cat payload.txt | scripts/gemini_fanout.py --jobs patch review tests alt --concurrency 3
```

## Architecture

### `gemini_delegate.py`

1. Reads stdin payload, inlines `--files` content into a structured header block (`GEMINI_DELEGATE_PAYLOAD v1`).
2. Invokes `gemini` CLI with a mode-specific system prompt, trying up to 4 flag-compatibility variants (json+plan → text+plan → json → text) to handle CLI version differences.
3. Cross-process rate limiting via `~/.cache/gemini-delegate/rate_limit.state` (flock-based), default 20s minimum between request starts (`--min-start-interval-sec`).
4. Retries transient failures (rate limits, timeouts, capacity errors) with exponential backoff within `--retry-window-sec`.
5. On capacity errors, auto-falls back from `--model pro` to `--fallback-model auto`.
6. With `--extract-diff`: strips the fenced ` ```diff ``` ` block from patch output and emits raw unified diff only.

### `gemini_fanout.py`

Wraps `gemini_delegate` with `asyncio`: spawns one subprocess per job, bounded by `--concurrency` (max 4) semaphore. Passes all retry/rate-limit args through. Prints a Markdown report per job to stdout.

## Key constants and defaults

| Setting | Default | Override |
|---|---|---|
| Model | `pro` | `--model` |
| Fallback model | `auto` | `--fallback-model` or `GEMINI_DELEGATE_FALLBACK_MODEL` env |
| Timeout | 900s | `--timeout-sec` |
| Retry window | 600s | `--retry-window-sec` |
| Min request interval | 20s | `--min-start-interval-sec` or `GEMINI_DELEGATE_MIN_START_INTERVAL_SEC` env |
| Max file inlining | 200 000 bytes | `--max-file-bytes` |
| Max concurrency (fanout) | 3 | `--concurrency` (capped at 4) |

## Install path (skill layout)

```
~/.agents/skills/gemini-delegate/
  SKILL.md
  scripts/
    gemini_delegate.py
    gemini_fanout.py
~/.local/bin/gemini-delegate  -> (symlink)
~/.local/bin/gemini-fanout    -> (symlink)
```

`SKILL.md` at the repo root is the machine-readable skill descriptor consumed by Codex and other agent hosts.
