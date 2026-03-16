#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Final, Iterable

DEFAULT_MODEL: Final[str] = "gemini-3-flash-preview"

QUERY_PATCH: Final[str] = (
    "You are a delegated senior engineer working on a well-scoped helper task.\n"
    "Use ONLY the provided payload as truth.\n"
    "OUTPUT RULES (MANDATORY):\n"
    "1) Output ONLY a unified diff in exactly ONE fenced block:\n"
    "```diff\n"
    "...diff...\n"
    "```\n"
    "2) No prose. No extra code blocks.\n"
)

QUERY_REVIEW: Final[str] = (
    "You are a delegated staff engineer reviewer for a well-specified helper task.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return concise bullet points: risks, edge cases, missing requirements, suggested improvements.\n"
)

QUERY_TESTS: Final[str] = (
    "You are a delegated test planner for a well-specified helper task.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return concise bullet points: concrete tests to add/run, including commands where appropriate.\n"
)

QUERY_ALT: Final[str] = (
    "You are a delegated architect for a well-specified helper task.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return 2-3 alternative approaches with tradeoffs. Be concrete.\n"
)

QUERY_RESEARCH: Final[str] = (
    "You are a delegated research helper for a well-specified, chunked task.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return concise bullet points covering findings, open questions, constraints, and a recommended next step.\n"
)

QUERY_ANSWER: Final[str] = (
    "You are a delegated answer helper for a well-specified, chunked task.\n"
    "Use ONLY the provided payload as truth.\n"
    "Return a concise plain-text answer. Use bullet points only when they improve clarity.\n"
)

MODE_TO_QUERY = {
    "patch": QUERY_PATCH,
    "review": QUERY_REVIEW,
    "tests": QUERY_TESTS,
    "alt": QUERY_ALT,
    "research": QUERY_RESEARCH,
    "answer": QUERY_ANSWER,
}

DIFF_FENCE_RE = re.compile(r"```diff\s*\n(.*?)\n```", re.DOTALL)
TRANSIENT_ERROR_RE = re.compile(
    r"(resource_exhausted|model_capacity_exhausted|rate.?limit|429|no capacity available|"
    r"timeout|timed out|temporar(?:y|ily) unavailable|service unavailable|econnreset|etimedout)",
    re.IGNORECASE,
)
CAPACITY_ERROR_RE = re.compile(r"(model_capacity_exhausted|no capacity available)", re.IGNORECASE)


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


def _run_once(cmd: list[str], payload: str, timeout_sec: int, live_stderr: bool) -> tuple[int, str, str]:
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _read_stream(stream, sink: list[str], stream_name: str) -> None:
        for line in iter(stream.readline, ""):
            sink.append(line)
            if live_stderr and stream_name == "stderr":
                print(f"INFO: gemini stderr: {line.rstrip()}", file=sys.stderr)
        stream.close()

    stdout_thread = threading.Thread(
        target=_read_stream,
        args=(process.stdout, stdout_lines, "stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(process.stderr, stderr_lines, "stderr"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    if process.stdin:
        process.stdin.write(payload)
        process.stdin.close()

    try:
        process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        timeout_msg = f"timeout after {timeout_sec}s"
        stderr_text = "".join(stderr_lines).rstrip()
        stderr_text = f"{stderr_text}\n{timeout_msg}" if stderr_text else timeout_msg
        return 124, "".join(stdout_lines), stderr_text

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    return process.returncode, "".join(stdout_lines), "".join(stderr_lines)


def _reserve_rate_limit_slot(min_start_interval_sec: float, rate_limit_file: str) -> None:
    if min_start_interval_sec <= 0:
        return
    parent = os.path.dirname(rate_limit_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(rate_limit_file, "a+", encoding="utf-8") as state:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        state.seek(0)
        raw = state.read().strip()
        last_ts = float(raw) if raw else 0.0
        now = time.time()
        wait_sec = min_start_interval_sec - (now - last_ts)
        if wait_sec > 0:
            print(
                f"INFO: global rate limit active, waiting {wait_sec:.1f}s before next Gemini request.",
                file=sys.stderr,
            )
            time.sleep(wait_sec)
            now = time.time()
        state.seek(0)
        state.truncate()
        state.write(f"{now:.6f}\n")
        state.flush()
        os.fsync(state.fileno())
        fcntl.flock(state.fileno(), fcntl.LOCK_UN)


def _unknown_option(stderr: str, stdout: str, opt_hint: str) -> bool:
    msg = f"{stderr}\n{stdout}".lower()
    return ("unknown option" in msg or "unrecognized option" in msg) and opt_hint.lower() in msg


def _run_gemini_once(
    query: str,
    payload: str,
    model: str,
    timeout_sec: int,
    live_stderr: bool,
    verbose: bool,
) -> tuple[int, str, str]:
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
        if verbose:
            print(
                f"INFO: invoking Gemini (model={model}, json={use_json}, plan={use_plan})",
                file=sys.stderr,
            )
        try:
            rc, out, err = _run_once(cmd, payload, timeout_sec, live_stderr=live_stderr)
        except FileNotFoundError:
            return 127, "", "`gemini` not found on PATH."

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


def _is_capacity_failure(out: str, err: str) -> bool:
    return bool(CAPACITY_ERROR_RE.search(f"{err}\n{out}"))


def _first_line(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0]


def _summarize_error(rc: int, out: str, err: str) -> str:
    combined = f"{err}\n{out}"
    message_match = re.search(r'"message":\s*"([^"]+)"', combined)
    if message_match:
        return message_match.group(1)
    status_match = re.search(r"\bstatus\b[^0-9]*(\d{3})", combined, re.IGNORECASE)
    if status_match:
        return f"status {status_match.group(1)}"
    return _first_line(err) or _first_line(out) or f"exit code {rc}"


def _run_gemini_with_retries(
    query: str,
    payload: str,
    model: str,
    fallback_model: str | None,
    timeout_sec: int,
    retry_window_sec: int,
    retry_initial_backoff_sec: float,
    retry_max_backoff_sec: float,
    min_start_interval_sec: float,
    rate_limit_file: str,
    live_stderr: bool,
    verbose: bool,
) -> tuple[int, str, str]:
    start = time.monotonic()
    attempt = 1
    active_model = model
    fallback_used = False
    fallback_reason = ""

    while True:
        _reserve_rate_limit_slot(min_start_interval_sec=min_start_interval_sec, rate_limit_file=rate_limit_file)
        if verbose:
            print(f"INFO: Gemini attempt {attempt} (model={active_model})", file=sys.stderr)
        rc, out, err = _run_gemini_once(
            query=query,
            payload=payload,
            model=active_model,
            timeout_sec=timeout_sec,
            live_stderr=live_stderr,
            verbose=verbose,
        )
        if rc == 0:
            if fallback_used and verbose:
                print(
                    f"INFO: model fallback was used ({model} -> {active_model}) due to {fallback_reason}. "
                    "Output quality may differ.",
                    file=sys.stderr,
                )
            return rc, out, err

        if fallback_model and active_model != fallback_model and _is_capacity_failure(out, err):
            if verbose:
                print(
                    f"INFO: capacity issue on model {active_model}; switching to fallback model {fallback_model}.",
                    file=sys.stderr,
                )
            fallback_used = True
            fallback_reason = "capacity limits on primary model"
            active_model = fallback_model
            attempt += 1
            continue

        elapsed = time.monotonic() - start
        remaining = retry_window_sec - elapsed
        if retry_window_sec <= 0 or not _is_transient_failure(rc, out, err) or remaining <= 0:
            return rc, out, err

        wait_sec = min(retry_initial_backoff_sec * (2 ** (attempt - 1)), retry_max_backoff_sec, remaining)
        reason = _summarize_error(rc, out, err)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex -> Gemini CLI delegate wrapper (headless).")
    parser.add_argument("--mode", choices=sorted(MODE_TO_QUERY.keys()), default="review")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model alias/name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--fallback-model",
        default=os.environ.get("GEMINI_DELEGATE_FALLBACK_MODEL", "auto"),
        help="Fallback model when the primary model has capacity errors (default: auto).",
    )
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument(
        "--retry-window-sec",
        type=int,
        default=600,
        help="Retry transient failures for up to this many seconds (default: 600).",
    )
    parser.add_argument(
        "--retry-initial-backoff-sec",
        type=float,
        default=5.0,
        help="Initial retry backoff in seconds for transient failures (default: 5).",
    )
    parser.add_argument(
        "--retry-max-backoff-sec",
        type=float,
        default=60.0,
        help="Maximum retry backoff in seconds (default: 60).",
    )
    parser.add_argument(
        "--min-start-interval-sec",
        type=float,
        default=float(os.environ.get("GEMINI_DELEGATE_MIN_START_INTERVAL_SEC", "20")),
        help="Minimum interval between Gemini request starts across processes (default: 20).",
    )
    parser.add_argument(
        "--rate-limit-file",
        default=os.path.expanduser("~/.cache/gemini-delegate/rate_limit.state"),
        help="Shared state file for global request pacing.",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=[],
        help="Inline these files into the payload (paths relative to CWD).",
    )
    parser.add_argument("--max-file-bytes", type=int, default=200_000)
    parser.add_argument("--extract-diff", action="store_true", help="Patch mode: print ONLY raw unified diff (strip fences).")
    parser.add_argument(
        "--no-live-stderr",
        action="store_true",
        help="Do not stream gemini stderr logs while requests are running.",
    )
    parser.add_argument("--quiet", action="store_true", help="Reduce delegate diagnostics.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    stdin_payload = sys.stdin.read()
    if not stdin_payload.strip():
        print("ERROR: Empty stdin payload. Provide TASK/CONTEXT via stdin.", file=sys.stderr)
        return 2

    query = MODE_TO_QUERY[args.mode]
    payload = _build_payload(stdin_payload, args.files, args.max_file_bytes)
    verbose = not args.quiet
    live_stderr = not args.no_live_stderr

    rc, out, err = _run_gemini_with_retries(
        query=query,
        payload=payload,
        model=args.model,
        fallback_model=args.fallback_model,
        timeout_sec=args.timeout_sec,
        retry_window_sec=args.retry_window_sec,
        retry_initial_backoff_sec=args.retry_initial_backoff_sec,
        retry_max_backoff_sec=args.retry_max_backoff_sec,
        min_start_interval_sec=args.min_start_interval_sec,
        rate_limit_file=args.rate_limit_file,
        live_stderr=live_stderr,
        verbose=verbose,
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
