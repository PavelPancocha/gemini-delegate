---
name: gemini-delegate
description: Delegate hard reasoning and PATCH proposals to Gemini CLI (headless). Gemini outputs unified diffs only; Codex applies edits and runs tests. Supports parallel fan-out runs (patch/review/tests/alt) and consolidated output.
---

## What this skill is for
Use when:
- Task is hard/ambiguous and you want a second high-quality brain.
- You want a unified diff proposal (Gemini outputs patch; Codex applies).
- You want parallel roles and then a consolidated summary.

## Safety boundary
- Gemini MUST NOT directly modify files.
- Gemini outputs diffs; Codex applies and validates.

## Practical workflow
- For patch: build payload and include file(s) with `--files`.
- Apply diff with `git apply --check` then `git apply`.
- If tests fail: feed error output back into another round.
