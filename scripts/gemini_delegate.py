#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Final, Iterable

QUERY_PATCH: Final[str] = (
    "You are a delegated senior engineer.\n"
    "Use ONLY the provided payload as truth.\n"
    "OUTPUT RULES (MANDATORY):\n"
    "1) Output ONLY a unified diff in exactly ONE fenced block:\n"
    "```diff\n"
    "...diff...\n"
    "```\n"
    "2) No prose. No extra code blocks.\n"
)

QUERY_REVIEW: Final[str] = (
    "You are a delegated staff engineer reviewer.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return concise bullet points: risks, edge cases, missing requirements, suggested improvements.\n"
)

QUERY_TESTS: Final[str] = (
    "You are a delegated test planner.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return concise bullet points: concrete tests to add/run, including commands where appropriate.\n"
)

QUERY_ALT: Final[str] = (
    "You are a delegated architect.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return 2-3 alternative approaches with tradeoffs. Be concrete.\n"
)

MODE_TO_QUERY = {
    "patch": QUERY_PATCH,
    "review": QUERY_REVIEW,
    "tests": QUERY_TESTS,
    "alt": QUERY_ALT,
}

DIFF_FENCE_RE = re.compile(r"```diff\s*\n(.*?)\n```", re.DOTALL)
TRANSIENT_ERROR_RE = re.compile(
    r"(resource_exhausted|model_capacity_exhausted|rate.?limit|429|no capacity available|"
    r"timeout|timed out|temporar(?:y|ily) unavailable|service unavailable|econnreset|etimedout)",
    re.IGNORECASE,
)


def _read_file(path: str, max_bytes: int) -> str:
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        return data[:max_bytes].decode("utf-8", errors="replace") + "\n\n[TRUNCATED]\n"
    return data.decode("utf-8", errors="replace")


def _git_root(cwd: str) -> str | None:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=2,
        )
        if p.returncode == 0:
            return p.stdout.strip() or None
    except Exception:
        return None
    return None


def _build_payload(stdin_payload: str, files: Iterable[str], max_file_bytes: int) -> str:
    cwd = os.getcwd()
    git_root = _git_root(cwd)
    hdr = [
        "# GEMINI_DELEGATE_PAYLOAD v1",
        f"CWD: {cwd}",
        f"GIT_ROOT: {git_root or ''}",
        "",
        "## STDIN_PAYLOAD",
        stdin_payload.rstrip(),
        "",
    ]
    file_blocks: list[str] = []
    for fpath in files:
        abspath = os.path.abspath(fpath)
        rel = os.path.relpath(abspath, cwd)
        try:
            content = _read_file(abspath, max_file_bytes)
        except FileNotFoundError:
            content = "[MISSING FILE]"
        file_blocks.append(f"--- {rel}\n{content.rstrip()}\n")
    if file_blocks:
        hdr.append("## FILES")
        hdr.extend(file_blocks)
    return "\n".join(hdr).rstrip() + "\n"


def _cmd_with_flags(query: str, model: str, include_json: bool, include_plan: bool) -> list[str]:
    cmd = ["gemini", "--model", model]
    if include_plan:
        cmd += ["--approval-mode", "plan"]
    if include_json:
        cmd += ["--output-format", "json"]
    # On this Gemini CLI build, --prompt is required for deterministic headless mode.
    cmd += ["--prompt", query]
    return cmd


def _run_once(cmd: list[str], payload: str, timeout_sec: int) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        input=payload,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout, p.stderr


def _unknown_option(stderr: str, stdout: str, opt_hint: str) -> bool:
    msg = f"{stderr}\n{stdout}".lower()
    return ("unknown option" in msg or "unrecognized option" in msg) and opt_hint.lower() in msg


def _run_gemini_once(query: str, payload: str, model: str, timeout_sec: int) -> tuple[int, str, str]:
    # Try strictest invocation first: model + read-only tools + json output.
    variants = [
        (True, True),   # json + plan
        (False, True),  # text + plan
        (True, False),  # json, no plan
        (False, False), # text only
    ]
    last: tuple[int, str, str] = (1, "", "")
    for use_json, use_plan in variants:
        cmd = _cmd_with_flags(query, model, include_json=use_json, include_plan=use_plan)
        try:
            rc, out, err = _run_once(cmd, payload, timeout_sec)
        except FileNotFoundError:
            return 127, "", "`gemini` not found on PATH."
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout_sec}s"

        last = (rc, out, err)
        if rc != 0:
            # Retry next variant only for option compatibility issues.
            if _unknown_option(err, out, "--output-format") or _unknown_option(err, out, "--approval-mode"):
                continue
            return rc, out, err

        if use_json:
            try:
                obj = json.loads(out)
                resp = obj.get("response")
                if isinstance(resp, str):
                    return 0, resp, err
            except Exception:
                # If JSON parsing fails, continue with raw output.
                return 0, out, err
            return 0, out, err

        return 0, out, err

    return last


def _is_transient_failure(rc: int, out: str, err: str) -> bool:
    if rc == 124:
        return True
    return bool(TRANSIENT_ERROR_RE.search(f"{err}\n{out}"))


def _first_line(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0]


def _run_gemini_with_retries(
    query: str,
    payload: str,
    model: str,
    timeout_sec: int,
    retry_window_sec: int,
    retry_initial_backoff_sec: float,
    retry_max_backoff_sec: float,
) -> tuple[int, str, str]:
    start = time.monotonic()
    attempt = 1

    while True:
        rc, out, err = _run_gemini_once(query, payload, model, timeout_sec)
        if rc == 0:
            return rc, out, err

        elapsed = time.monotonic() - start
        remaining = retry_window_sec - elapsed
        if retry_window_sec <= 0 or not _is_transient_failure(rc, out, err) or remaining <= 0:
            return rc, out, err

        wait_sec = min(retry_initial_backoff_sec * (2 ** (attempt - 1)), retry_max_backoff_sec, remaining)
        reason = _first_line(err) or _first_line(out) or f"exit code {rc}"
        print(
            f"INFO: transient Gemini failure on attempt {attempt}: {reason}. "
            f"Retrying in {wait_sec:.1f}s (remaining retry window {remaining:.1f}s).",
            file=sys.stderr,
        )
        time.sleep(wait_sec)
        attempt += 1


def _extract_diff(text: str) -> str | None:
    m = DIFF_FENCE_RE.search(text)
    if not m:
        return None
    return m.group(1).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Codex -> Gemini CLI delegate wrapper (headless).")
    ap.add_argument("--mode", choices=sorted(MODE_TO_QUERY.keys()), default="review")
    ap.add_argument("--model", default="pro", help="Gemini model alias/name (default: pro)")
    ap.add_argument("--timeout-sec", type=int, default=900)
    ap.add_argument(
        "--retry-window-sec",
        type=int,
        default=600,
        help="Retry transient failures for up to this many seconds (default: 600).",
    )
    ap.add_argument(
        "--retry-initial-backoff-sec",
        type=float,
        default=5.0,
        help="Initial retry backoff in seconds for transient failures (default: 5).",
    )
    ap.add_argument(
        "--retry-max-backoff-sec",
        type=float,
        default=60.0,
        help="Maximum retry backoff in seconds (default: 60).",
    )
    ap.add_argument("--files", nargs="*", default=[], help="Inline these files into the payload (paths relative to CWD).")
    ap.add_argument("--max-file-bytes", type=int, default=200_000)
    ap.add_argument("--extract-diff", action="store_true", help="Patch mode: print ONLY raw unified diff (strip fences).")
    args = ap.parse_args()

    stdin_payload = sys.stdin.read()
    if not stdin_payload.strip():
        print("ERROR: Empty stdin payload. Provide TASK/CONTEXT via stdin.", file=sys.stderr)
        return 2

    query = MODE_TO_QUERY[args.mode]
    payload = _build_payload(stdin_payload, args.files, args.max_file_bytes)

    rc, out, err = _run_gemini_with_retries(
        query=query,
        payload=payload,
        model=args.model,
        timeout_sec=args.timeout_sec,
        retry_window_sec=args.retry_window_sec,
        retry_initial_backoff_sec=args.retry_initial_backoff_sec,
        retry_max_backoff_sec=args.retry_max_backoff_sec,
    )
    if rc != 0:
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        else:
            print(f"ERROR: gemini exited with code {rc}", file=sys.stderr)
        return rc

    if args.mode == "patch" and args.extract_diff:
        diff = _extract_diff(out)
        if not diff:
            print("ERROR: No ```diff``` fenced block found in Gemini output.", file=sys.stderr)
            print(out.rstrip(), file=sys.stderr)
            return 3
        sys.stdout.write(diff)
        return 0

    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
