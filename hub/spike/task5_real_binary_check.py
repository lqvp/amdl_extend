"""Task 5: run the real `wrapper/wrapper-lite-rootless` through `WrapperSupervisor`.

Deliberately *not* a pytest test. The real launcher needs a rootfs, 6-19 s to reach a
serving state (spike §7) and an account for `/status` to report regions -- none of which
belongs in a hermetic suite -- so this is a one-shot harness whose output is transcribed
into the task-5 report. Task 10's acceptance run is the real end-to-end.

    cd hub && uv run python spike/task5_real_binary_check.py

It reports: the pre-flight verdict on the configured port, the time to a 200 on
`/status`, whether the readiness gate was satisfied, and whether `stop()` left anything
alive. Read-only with respect to `wrapper/` apart from the `rootfs/data` the launcher
creates for itself: `--base-dir data` is a chroot-relative segment, so it names the
directory the launcher already makes.

Note on `--login`: a real 2FA exchange cannot be exercised here. It needs an Apple account
with 2FA enabled, and the credentials would end up in this process's own argv and in
`/proc/<pid>/cmdline` for the lifetime of the login child.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.wrapper_supervisor import SupervisorError, WrapperSupervisor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BINARY = REPO_ROOT / "wrapper" / "wrapper-lite-rootless"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--host", default="127.0.0.1")
    # 0 by default: this host already runs a wrapper on 12340 (spike §6.5), and the
    # pre-flight would adopt it -- which is the correct behaviour and tells us nothing
    # about whether *we* can start one.
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--login", metavar="USER:PASS", help="drive the 2FA exchange")
    parser.add_argument(
        "--code", help="the 2FA code, to be submitted once a challenge comes back"
    )
    args = parser.parse_args()

    print(f"binary           : {args.binary}")
    print(f"exists/executable: {args.binary.is_file()} / {os.access(args.binary, os.X_OK)}")
    print(f"host:port        : {args.host}:{args.port or '(ephemeral)'}")
    print(f"adopt_existing   : {args.adopt_existing}")
    print()

    supervisor = WrapperSupervisor(
        binary=args.binary,
        # The launcher chroots into ./rootfs relative to its CWD and resolves --base-dir
        # after chroot("."), so this is a single chroot-relative segment: it lands in
        # wrapper/rootfs/data, which the launcher creates for itself. An absolute host
        # path here would mean something else entirely once inside the chroot.
        base_dir=Path("data"),
        host=args.host,
        port=args.port,
        log_sink=lambda line: print(f"  | {line}", flush=True),
        startup_timeout=args.startup_timeout,
        adopt_existing=args.adopt_existing,
    )

    started = time.monotonic()
    ready = False
    try:
        await supervisor.start()
        ready = True
        print(f"\nstart() returned after {time.monotonic() - started:.1f}s")
        print(f"  running : {supervisor.running}")
        print(f"  adopted : {supervisor.adopted}")
        print(f"  pid     : {supervisor.pid}")
        print(f"  port    : {supervisor.bound_port}")
        print(f"  status  : {await supervisor.status()}")
    except SupervisorError as exc:
        print(f"\nstart() raised after {time.monotonic() - started:.1f}s:\n{exc}")
    except TimeoutError:
        print(f"\nstart() itself hung after {time.monotonic() - started:.1f}s")

    if args.login:
        if args.adopt_existing:
            print(
                "\n--login is not attempted: an adopted wrapper is not ours to log in, and "
                "login() refuses it"
            )
        else:
            username, _, password = args.login.partition(":")
            try:
                challenge = await supervisor.login(username, password)
                print(
                    f"\n2FA challenge {challenge.id[:8]} expires at "
                    f"{challenge.expires_at:.0f} "
                    f"({challenge.expires_at - time.time():.0f}s from now)"
                )
                if args.code:
                    await supervisor.submit_2fa(challenge.id, args.code)
                    print("2FA code written to the file the login child is polling for")
                else:
                    print("no --code given, so nothing was submitted")
            except SupervisorError as exc:
                print(f"\nlogin() raised: {exc}")

    pid = supervisor.pid
    await supervisor.stop()
    print(f"\nstop() returned after {time.monotonic() - started:.1f}s from spawn")
    print(f"  running : {supervisor.running}")
    print(f"  adopted : {supervisor.adopted}")

    if pid is None:
        print("  pid     : none -- nothing was spawned, so nothing can be orphaned")
        return 0 if ready else 1

    # A moment's grace before the liveness check: `stop()` has already reaped the child, so
    # this is belt and braces against a pid being recycled under us.
    await asyncio.sleep(0.5)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"  pid {pid} : gone, reaped. No orphan.")
        return 0 if ready else 1
    print(f"  pid {pid} : STILL ALIVE -- an orphan. This is R7 failing.")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
