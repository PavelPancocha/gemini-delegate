# gemini-delegate

A small, patch-first delegation layer for using Gemini CLI from Codex/CLI workflows.

## Motivation

When you use LLMs in coding workflows, two problems show up quickly:

1. You want help from another model, but you do not want that model to edit your files directly.
2. You want to ask for multiple perspectives (patch, review, tests, alternatives) without manually running four separate prompts.

`gemini-delegate` addresses both:

- Gemini runs headlessly from stdin payloads.
- Gemini outputs diffs; your local workflow applies changes.
- Parallel fan-out runs gather multiple roles in one command.

## What it does

- `scripts/gemini_delegate.py`
  - Single delegated run in one of four modes: `patch`, `review`, `tests`, `alt`
  - Supports `--files` to inline local file content into the prompt payload
  - In `patch` mode, supports `--extract-diff` to emit raw unified diff only
  - Retries transient provider failures (timeouts/capacity/rate-limit)

- `scripts/gemini_fanout.py`
  - Launches multiple delegate modes in parallel
  - Prints one consolidated report with return code and output per role

## Safety boundary

- Gemini does **not** edit files directly.
- Patch mode requires one fenced `diff` block.
- You apply edits explicitly (`git apply --check` then `git apply`).

## Install

Requirements:

- Python 3.10+
- `gemini` CLI installed and authenticated

### Manual install (recommended)

```bash
git clone https://github.com/PavelPancocha/gemini-delegate.git
cd gemini-delegate

mkdir -p ~/.agents/skills/gemini-delegate/scripts ~/.local/bin
cp SKILL.md ~/.agents/skills/gemini-delegate/SKILL.md
cp scripts/gemini_delegate.py ~/.agents/skills/gemini-delegate/scripts/gemini_delegate.py
cp scripts/gemini_fanout.py ~/.agents/skills/gemini-delegate/scripts/gemini_fanout.py
chmod +x ~/.agents/skills/gemini-delegate/scripts/gemini_delegate.py ~/.agents/skills/gemini-delegate/scripts/gemini_fanout.py

ln -sfn ~/.agents/skills/gemini-delegate/scripts/gemini_delegate.py ~/.local/bin/gemini-delegate
ln -sfn ~/.agents/skills/gemini-delegate/scripts/gemini_fanout.py ~/.local/bin/gemini-fanout

gemini-delegate --help
gemini-fanout --help
```

### One-shot prompt for Codex (install from GitHub)

Paste this into Codex:

```text
Install the `gemini-delegate` skill from https://github.com/PavelPancocha/gemini-delegate into my user scope only.

Requirements:
- Create/update: ~/.agents/skills/gemini-delegate/
- Place SKILL.md at ~/.agents/skills/gemini-delegate/SKILL.md
- Place scripts in ~/.agents/skills/gemini-delegate/scripts/
- Make scripts executable
- Create/update symlinks:
  - ~/.local/bin/gemini-delegate -> ~/.agents/skills/gemini-delegate/scripts/gemini_delegate.py
  - ~/.local/bin/gemini-fanout -> ~/.agents/skills/gemini-delegate/scripts/gemini_fanout.py
- Do not modify any project/repo files
- Verify with:
  - gemini-delegate --help
  - gemini-fanout --help
```

## Quick start

Single review pass:

```bash
echo "TASK: list 3 edge cases for CSV quoted commas" \
  | scripts/gemini_delegate.py --mode review
```

Patch proposal and apply:

```bash
cat payload.txt \
  | scripts/gemini_delegate.py --mode patch --files path/to/file.py --extract-diff > /tmp/patch.diff

git apply --check /tmp/patch.diff
git apply /tmp/patch.diff
```

Parallel fan-out:

```bash
cat payload.txt \
  | scripts/gemini_fanout.py --jobs patch review tests alt --concurrency 3
```

## Retry controls

Both scripts expose retry controls for transient provider instability:

- `--timeout-sec`
- `--retry-window-sec`
- `--retry-initial-backoff-sec`
- `--retry-max-backoff-sec`
- `--min-start-interval-sec`
- `--rate-limit-file`

Example:

```bash
echo "TASK: ..." \
  | scripts/gemini_delegate.py --mode patch --timeout-sec 60 --retry-window-sec 900
```

## Concurrency throttle

By default, requests are globally paced to one Gemini request start every 20 seconds across processes.

This protects against provider-side concurrent request limits, especially when using `gemini_fanout.py`.

## Notes

- If provider capacity is unavailable, retries continue until `--retry-window-sec` is exhausted.
- Path formatting of model-produced diffs can vary; apply with `-p0` or default strip as needed.

## License

MIT — see [LICENSE](./LICENSE)
