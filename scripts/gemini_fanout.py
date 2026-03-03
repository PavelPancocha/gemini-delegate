#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

JOBS = ["patch", "review", "tests", "alt"]


@dataclass(frozen=True)
class JobResult:
    job: str
    rc: int
    out: str
    err: str


async def run_one(
    job: str,
    payload: str,
    model: str,
    timeout_sec: int,
    retry_window_sec: int,
    retry_initial_backoff_sec: float,
    retry_max_backoff_sec: float,
    min_start_interval_sec: float,
    rate_limit_file: str,
) -> JobResult:
    cmd = [
        "gemini-delegate",
        "--mode",
        job,
        "--model",
        model,
        "--timeout-sec",
        str(timeout_sec),
        "--retry-window-sec",
        str(retry_window_sec),
        "--retry-initial-backoff-sec",
        str(retry_initial_backoff_sec),
        "--retry-max-backoff-sec",
        str(retry_max_backoff_sec),
        "--min-start-interval-sec",
        str(min_start_interval_sec),
        "--rate-limit-file",
        rate_limit_file,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        total_timeout = timeout_sec + retry_window_sec + 10
        out_b, err_b = await asyncio.wait_for(proc.communicate(payload.encode("utf-8")), timeout=total_timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return JobResult(job=job, rc=124, out="", err=f"timeout after {timeout_sec}s")
    rc = proc.returncode or 0
    return JobResult(
        job=job,
        rc=rc,
        out=out_b.decode("utf-8", errors="replace"),
        err=err_b.decode("utf-8", errors="replace"),
    )


async def runner(
    jobs: list[str],
    payload: str,
    concurrency: int,
    model: str,
    timeout_sec: int,
    retry_window_sec: int,
    retry_initial_backoff_sec: float,
    retry_max_backoff_sec: float,
    min_start_interval_sec: float,
    rate_limit_file: str,
) -> list[JobResult]:
    sem = asyncio.Semaphore(concurrency)

    async def wrapped(job: str) -> JobResult:
        async with sem:
            return await run_one(
                job,
                payload,
                model,
                timeout_sec,
                retry_window_sec,
                retry_initial_backoff_sec,
                retry_max_backoff_sec,
                min_start_interval_sec,
                rate_limit_file,
            )

    tasks = [asyncio.create_task(wrapped(job)) for job in jobs]
    return await asyncio.gather(*tasks)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run multiple Gemini delegate jobs in parallel (fan-out).")
    ap.add_argument("--jobs", nargs="+", default=["review"], choices=JOBS)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout-sec", type=int, default=900)
    ap.add_argument("--model", default="pro")
    ap.add_argument("--retry-window-sec", type=int, default=600)
    ap.add_argument("--retry-initial-backoff-sec", type=float, default=5.0)
    ap.add_argument("--retry-max-backoff-sec", type=float, default=60.0)
    ap.add_argument("--min-start-interval-sec", type=float, default=20.0)
    ap.add_argument(
        "--rate-limit-file",
        default=os.path.expanduser("~/.cache/gemini-delegate/rate_limit.state"),
        help="Shared state file for global request pacing.",
    )
    args = ap.parse_args()
    if args.concurrency < 1 or args.concurrency > 4:
        print("ERROR: --concurrency must be between 1 and 4.", file=sys.stderr)
        return 2

    payload = sys.stdin.read()
    if not payload.strip():
        print("ERROR: Empty stdin payload.", file=sys.stderr)
        return 2

    results = asyncio.run(
        runner(
            args.jobs,
            payload,
            args.concurrency,
            args.model,
            args.timeout_sec,
            args.retry_window_sec,
            args.retry_initial_backoff_sec,
            args.retry_max_backoff_sec,
            args.min_start_interval_sec,
            args.rate_limit_file,
        )
    )

    for result in results:
        print(f"## Gemini job: {result.job} (rc={result.rc})")
        if result.rc != 0 and result.err.strip():
            print("**stderr:**")
            print("```")
            print(result.err.rstrip())
            print("```")
        if result.out.strip():
            print(result.out.rstrip())
        print()

    return 0 if all(result.rc == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
