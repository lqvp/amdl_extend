#!/usr/bin/env python3
"""Run one test N times under CPU oversubscription, and report failures.

The oversubscription is the point. `test_a_crashed_wrapper_is_restarted` passed 25/25 in
isolation while failing 20/20 under 2x CPU -- so "it passed once" is not evidence that a
timing-sensitive test is sound, and the only way to tell a real race from a slow machine is to
make the machine slow deliberately and see whether the failure appears.

    uv run python hub/deploy/oversubscribe_check.py test_supervisor.py::test_a_crashed_wrapper_is_restarted
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

DEFAULT_RUNS = 20


def load_workers() -> int:
    """2x the host's CPUs, which is what "oversubscribed" has to mean to starve a loop.

    A fixed count is wrong on a small machine and worse on a large one: 8 busy loops on 16
    CPUs is *not* load, it is a warm-up. Measured here: 0/20 "failures" with 8 workers on 16
    CPUs that turned out to be this harness failing to find the test at all, and 1 pass in
    1.26 s with 32. The number has to be derived from `os.cpu_count()`.
    """
    return max(2, 2 * (os.cpu_count() or 4))


def spawn_load() -> list[subprocess.Popen]:
    """Background CPU hogs, so the event loop is starved the way a loaded host starves it."""
    workers = [
        subprocess.Popen([sys.executable, "-c", "while True: pass"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(load_workers())
    ]
    time.sleep(0.3)
    return workers


def main() -> int:
    target = sys.argv[1]
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_RUNS
    cpus = os.cpu_count() or 4
    print(f"target      : {target}")
    print(f"host cpus   : {cpus}")
    print(f"load        : {load_workers()} busy loops (~2x oversubscription)")
    print(f"runs        : {runs}")
    print()

    load = spawn_load()
    failures = 0
    broken = 0
    try:
        for run in range(1, runs + 1):
            result = subprocess.run(
                ["uv", "run", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", target],
                cwd="hub", capture_output=True, text=True, timeout=300,
            )
            # **A harness that cannot find the test must not be reported as a failing test.**
            # That mistake was made here once already: a bad target path plus a bad cwd made
            # every run exit non-zero on a collection error, and the output read `0/20 passed`
            # as though the fix had not worked. It is indistinguishable from a real failure
            # unless it is checked for, so it is checked for.
            harness = "no tests ran" in result.stdout or "file or directory not found" in (
                result.stdout + result.stderr
            )
            ok = result.returncode == 0 and not harness
            failures += not ok
            broken += harness
            tail = next(
                (line for line in result.stdout.splitlines() if "passed" in line or "failed" in line
                 or "no tests ran" in line),
                "(no summary line)",
            )
            verdict = "HARNESS" if harness else ("pass" if ok else "FAIL")
            print(f"  run {run:>2}/{runs}: {verdict:<7} {tail.strip()[:80]}")
            if not ok and not harness:
                for line in result.stdout.splitlines():
                    if "Error" in line or "assert" in line.lower():
                        print(f"           {line.strip()[:110]}")
                        break
    finally:
        for worker in load:
            try:
                os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                worker.kill()
        for worker in load:
            worker.wait()

    print()
    if broken:
        print(f"HARNESS FAULT: {broken}/{runs} runs never reached the test. The number above is "
              f"meaningless.")
        return 3
    print(f"{runs - failures}/{runs} passed under oversubscription")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
