#!/usr/bin/env python3
"""SPIKE: can `wrapper-lite-rootless` be supervised as an ordinary child process?

This is a throwaway probe, not production code.  It answers one question and
prints one verdict line:

    VERDICT: works           -- every check below passed
    VERDICT: needs-fallback  -- at least one did not; the topology must change

The verdict is computed as the AND of *every* recorded check, so it can never
contradict a `FAIL` printed above it.  When it is `needs-fallback` the probe
also names the checks that failed, because "needs-fallback" can be caused by a
genuine topology problem (the launcher cannot be a child) or by a broken harness
(the probe was PID 1, the port was taken, the rootfs was not writable).  Only
the first kind is a finding about the topology.

Why the question matters (spec §3, §15 row 1): the whole single-container
topology rests on the web app spawning the launcher itself, instead of a
separate container.  If the launcher needs PID 1 or `privileged`, Task 5 has to
build the two-container topology and the 2FA UX degrades (spec §3.1).

Two facts about the upstream launcher drive the harness (see
wrapper/wrapper-lite-rootless.c):

  * It chroots into `./rootfs` -- a path **relative to the current working
    directory**, not to `--base-dir`.  So the probe must run the launcher with
    `cwd` set to the directory that contains `rootfs/`, exactly like upstream's
    `entrypoint.sh` does (`./wrapper-lite-rootless` from `/app`).
  * `--base-dir` is resolved *after* `chroot(".")`, so it is always relative to
    the chroot root: `/data` means `<cwd>/rootfs/data`.  The probe therefore
    passes a unique relative name and cleans up the directory it creates.

What this probe does NOT verify: the 2FA exchange.  It proves only that the
launcher's stdin accepts a write without disturbing it.  No code was submitted
and no prompt was detected, so spec §3.1's stdin-driven 2FA remains unverified.

Usage:
    uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 0
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Deadline for the listen banner, per the task contract.
BANNER_TIMEOUT_S = 30.0
# How long to keep watching the child after the banner, to prove it did not
# exit immediately (the probe being a non-init parent is the risk being tested).
SETTLE_S = 3.0
# The exact banner the payload prints on reaching httplib::Server::listen():
#   LOG_INFO("wrapper-lite listening on %s:%d", g_host.c_str(), g_port);
# at wrapper/lite/lite_main.cpp:701.  Anchored on that full phrase rather than
# a bare "listen" (which would also match an error line) or the literal port
# (which is meaningless under --port 0).
BANNER_MARKER = b"wrapper-lite listening on"
# The one socket option the payload's listening socket sets.  httplib's
# default_socket_options() (httplib.h:2039-2055) takes the `#ifdef SO_REUSEPORT`
# branch on Linux, so this is SO_REUSEPORT and *not* SO_REUSEADDR; the pre-flight
# in can_bind() models exactly that, because nothing else about the payload's
# bind is knowable from outside.
REUSEPORT = getattr(socket, "SO_REUSEPORT", 0)
# The launcher's own perror() strings for the user-namespace creation and
# mapping steps (wrapper-lite-rootless.c:44-67).  A hit means the userns was
# refused outright.
UNSHARE_ERRORS = (b"unshare:", b"uid_map", b"setgroups", b"gid_map")
# The launcher's own perror() strings for the steps that need CAP_SYS_ADMIN
# *inside* the new userns (wrapper-lite-rootless.c:103-138): mkdir/open of the
# dev and proc mountpoints, the two mount() calls, and chdir/chroot.  A hit means
# the userns was created but the mount namespace work inside it was refused.
# Every perror() on that line range is listed -- an omission here would make a
# real userns failure report PASS.  Each comment gives the line of the *failing
# call*; the perror() that prints the string is the next line (104, 110, 116,
# 121, 126, 132, 136), which is why grepping for the perror line lands one line
# below the marker.
#
# Deliberately NOT listed, though the launcher does have them: perror("signal")
# at line 81 and perror("fork") at line 93 sit between the two ranges above;
# perror("mkdir base_dir_arg failed") at 143 and perror("mkdir mpl_db failed") at
# 149 are non-fatal (no `return 1`); and perror("execve") at 154 is post-chroot.
# None of the five is a namespace refusal, and a launcher that dies at execve
# already fails the banner and liveness checks, so attributing them to this
# check would be wrong.
NAMESPACE_MOUNT_ERRORS = (
    b"mkdir ./rootfs/dev failed",         # line 103
    b"open ./rootfs/dev/urandom failed",  # line 108
    b"mount /dev/urandom failed",         # line 115
    b"mkdir ./rootfs/proc failed",        # line 120
    b"mount proc failed",                 # line 125
    b"chdir ./rootfs failed",             # line 131
    b"chroot . failed",                   # line 135
)

# Every check recorded here gates the verdict.  Order is the order printed.
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read_proc_field(path: str, field: str) -> str:
    """Read a whitespace-separated field out of a /proc file, e.g. 'Seccomp'."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == f"{field}:":
                    return parts[1]
    except OSError:
        pass
    return "n/a"


def read_proc_text(path: str) -> str:
    """Read a whole /proc file, degrading to 'n/a' rather than raising.

    /proc entries can be unreadable (absent, masked, permission denied) in a
    container.  The probe must still reach a verdict, so every diagnostic read
    goes through here instead of an unguarded open().
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError as exc:
        return f"<unreadable: {exc.strerror or exc}>"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def can_connect(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _bind_succeeds(port: int, host: str, opt: int) -> bool:
    """One bind() attempt, optionally with a single socket option set."""
    with socket.socket() as sock:
        if opt:
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, 1)
            except OSError:
                return False
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def can_bind(port: int, host: str = "127.0.0.1") -> tuple[bool, str]:
    """Can the payload bind host:port?  Returns (verdict, reason-if-refused).

    A pre-existing listener makes `svr.listen()` fail with EADDRINUSE, return
    immediately, and -- via lite_main.cpp:705
    `pthread_kill(sig_thread.native_handle(), SIGTERM)` -- make the payload log
    "received signal 15" and exit 0 right after printing a successful-looking
    banner.  That is indistinguishable from an external kill in the logs, so
    refuse to start rather than misreport it.

    The payload's listening socket takes exactly one option: SO_REUSEPORT.
    httplib's `default_socket_options()` (httplib.h:2039-2055) takes the
    `#ifdef SO_REUSEPORT` branch on Linux and never sets SO_REUSEADDR.  That one
    fact decides this pre-flight, and the matrix below is measured on this host
    rather than assumed:

      what holds the port                  plain  +REUSEADDR  +REUSEPORT
      -----------------------------------  -----  ----------  ----------
      nothing                                OK       OK          OK
      TIME_WAIT from an earlier `lite`    EADDR    EADDR        OK
      a live listener with SO_REUSEPORT    EADDR    EADDR        OK
      a live listener with no options      EADDR    EADDR      EADDR

    So SO_REUSEADDR changes nothing in any row: the kernel honours it over
    TIME_WAIT only when the *conflicting* socket set it too, and the payload
    never does.  SO_REUSEPORT is the only option that moves the outcome, and it
    is the option the payload itself uses.

    The two rows it rescues still have to be told apart, because they mean
    opposite things.  A TIME_WAIT remnant is harmless -- the next bind succeeds
    too, which is why a second probe run on the same fixed port works.  A live
    SO_REUSEPORT listener is a stale launcher, and the payload would end up
    *sharing* the port with it, after which `/status` can be answered by the
    wrong process.  A connect() separates them: a TIME_WAIT port has no
    listener and refuses the connection.
    """
    if _bind_succeeds(port, host, 0):
        return True, ""
    if not _bind_succeeds(port, host, REUSEPORT):
        return False, (
            "a live listener that does not set SO_REUSEPORT holds it, so the "
            "payload could not bind there either (this host's qemu wrapper on "
            "12340 is one such listener)"
        )
    if can_connect(port):
        return False, (
            "a live listener that sets SO_REUSEPORT holds it; the payload would "
            "share the port and /status could then be answered by that stale "
            "process instead of the one under test"
        )
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--binary", required=True, help="path to wrapper-lite-rootless")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=12340, help="0 picks a free ephemeral port")
    ap.add_argument("--keep-base-dir", action="store_true", help="do not delete <rootfs>/<base-dir>")
    args = ap.parse_args()

    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        print(f"FATAL: {binary} is not an executable file", file=sys.stderr)
        return 3
    # The launcher chroots into ./rootfs, so the CWD must be the binary's parent.
    run_dir = os.path.dirname(binary)
    if not os.path.isdir(os.path.join(run_dir, "rootfs", "system", "bin")):
        print(
            f"FATAL: {os.path.join(run_dir, 'rootfs')} does not look like a rootfs; "
            "the launcher chroots into ./rootfs relative to its CWD",
            file=sys.stderr,
        )
        return 3

    port = free_port() if args.port == 0 else args.port
    # (This host already runs a wrapper QEMU instance on 127.0.0.1:12340, so the
    # default port is not usable here.  In the shipped topology 12340 is
    # container-internal and never published, so a collision can only be a stale
    # launcher inside one container -- which the /status gate already covers.)
    bindable, why_not = can_bind(port)
    if not bindable:
        print(
            f"HARNESS FAULT: {args.host}:{port} is already in use, so the probe would "
            f"measure EADDRINUSE instead of the question -- {why_not}. "
            "Re-run with --port 0 to pick a free ephemeral port.",
            file=sys.stderr,
        )
        return 4

    base_dir = f"spike-probe-{os.getpid()}"
    base_dir_abs = os.path.join(run_dir, "rootfs", base_dir)
    os.makedirs(base_dir_abs, exist_ok=True)

    print("=== environment ===")
    print(f"python           : {sys.version.split()[0]}  ({sys.executable})")
    print(f"probe pid/ppid   : {os.getpid()} / {os.getppid()}")
    print(f"binary           : {binary}")
    print(f"cwd for launcher : {run_dir}")
    print(f"--base-dir       : {base_dir}  ->  {base_dir_abs} (inside the chroot)")
    print(f"--host/--port    : {args.host}:{port}")
    print(f"Seccomp          : {read_proc_field('/proc/self/status', 'Seccomp')} (0 = unconfined)")
    print(f"CapEff           : {read_proc_field('/proc/self/status', 'CapEff')}")
    print(f"NoNewPrivs       : {read_proc_field('/proc/self/status', 'NoNewPrivs')}")
    print(f"uid_map          : {read_proc_text('/proc/self/uid_map')!r}")
    print(f"pid 1 is         : {read_proc_text('/proc/1/comm')!r}")
    print()

    argv = [binary, "--base-dir", base_dir, "--host", args.host, "--port", str(port)]
    print("=== spawning ===")
    print("$ " + " ".join(argv))
    print(f"(cwd={run_dir})")
    print()

    # --- contract clause 1: exactly this Popen shape -----------------------
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        cwd=run_dir,
    )

    chunks: list[bytes] = []
    done = threading.Event()

    def reader() -> None:
        # os.read() returns whatever has arrived; BufferedReader.read(n) blocks
        # until n bytes or EOF, so a long-lived child would produce no output at
        # all until it shut down.
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        done.set()

    threading.Thread(target=reader, daemon=True).start()

    # --- contract clause 2: not pid 1, and our parent is not init ---------
    try:
        assert proc.pid != 1, f"launcher is pid 1 (pid={proc.pid})"
        assert os.getppid() != 0, f"probe was reparented to init (getppid()=0, pid={os.getpid()})"
        record("launcher is not pid 1", True, f"pid={proc.pid}, parent probe pid={os.getpid()}")
    except AssertionError as exc:
        record("launcher is not pid 1 / probe parent is not init", False, str(exc))

    # --- contract clause 3: the listen banner within 30 s ------------------
    deadline = time.monotonic() + BANNER_TIMEOUT_S
    banner_at: float | None = None
    while time.monotonic() < deadline:
        if BANNER_MARKER in b"".join(chunks):
            banner_at = time.monotonic()
            break
        if proc.poll() is not None or done.is_set():
            break
        time.sleep(0.1)

    # A child that failed fast writes its perror() line and exits immediately.
    # Without this drain the reader thread may not have appended that line yet,
    # and the two error scans below would see an empty buffer and PASS wrongly.
    if proc.poll() is not None or done.is_set():
        done.wait(timeout=5.0)

    out = b"".join(chunks)
    record(
        f"listen banner within {BANNER_TIMEOUT_S:.0f}s",
        banner_at is not None,
        f"marker={BANNER_MARKER.decode()!r}"
        if banner_at is not None
        else f"exit={proc.poll()}, output={len(out)}B",
    )

    # --- contract clause 4: the namespace steps were not refused ------------
    # Split by stage so a run says *which* namespace operation failed: the userns
    # itself, or the privileged-inside-the-userns mount work.
    unshare_hits = [e.decode() for e in UNSHARE_ERRORS if e in out]
    record("user namespace was created (no unshare/uid_map refusal)", not unshare_hits, ", ".join(unshare_hits))

    mount_hits = [e.decode() for e in NAMESPACE_MOUNT_ERRORS if e in out]
    record("namespace mounts succeeded (no proc/urandom/chroot refusal)", not mount_hits, ", ".join(mount_hits))

    # --- supplementary: it must still be alive, and the port must accept ---
    alive = proc.poll() is None
    record("still alive after the banner", alive, f"returncode={proc.poll()}")
    if banner_at is not None and not alive:
        print(
            "HARNESS NOTE: the banner was printed but the process is already gone. "
            "A failing svr.listen() returns at once and the main thread then "
            "self-signals the sigwait thread, which logs 'received signal 15'. "
            "The pre-flight in can_bind() ruled out the two causes it can see: "
            "the port was bindable outright, and nothing was listening on it, so "
            "there is neither a stale listener nor a TIME_WAIT remnant. "
            "The launcher does NOT unshare a network namespace "
            "(wrapper-lite-rootless.c:44 has no CLONE_NEWNET), so the bind "
            "competes in the netns it inherited. Look for something that took the "
            "port in between the pre-flight and the launcher's own bind."
        )

    if alive:
        time.sleep(SETTLE_S)
        alive = proc.poll() is None
        record(f"still alive {SETTLE_S:.0f}s later", alive, f"returncode={proc.poll()}")
    else:
        # Always record, so a failing run reports every check rather than a
        # shorter list that looks like a pass.
        record(f"still alive {SETTLE_S:.0f}s later", False, "child already exited; not attempted")

    connectable = alive and can_connect(port)
    record("loopback port accepts a TCP connection", connectable, f"{args.host}:{port}")

    # --- supplementary: the HTTP server must actually answer ---------------
    # A bare TCP connect only proves bind()+listen() succeeded.  GET /status is
    # the endpoint the client health-checks first (wrapper/lite/lite_main.cpp),
    # and it needs no tokens, so it isolates "the server works" from
    # "the server is logged in".
    http_status = None
    http_body = ""
    if connectable:
        try:
            with urllib.request.urlopen(f"http://{args.host}:{port}/status", timeout=5) as resp:
                http_status = resp.status
                http_body = resp.read(200).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            http_body = f"<error: {exc}>"
    record(
        "GET /status answers",
        http_status == 200,
        f"HTTP {http_status} {http_body}" if http_status else http_body,
    )

    # --- supplementary: stdin is writable (NOT a 2FA exchange) ------------
    # Writes a bare newline and checks the child survives.  It does NOT
    # demonstrate that a 2FA prompt can be detected or a code submitted.
    stdin_ok = False
    stdin_detail = "child already exited; not attempted"
    if alive:
        try:
            assert proc.stdin is not None
            proc.stdin.write(b"\n")
            proc.stdin.flush()
            proc.stdin.close()
            time.sleep(0.5)
            stdin_ok = proc.poll() is None
            stdin_detail = "wrote b'\\n', closed stdin, child survived" if stdin_ok else "child died after the write"
        except (OSError, ValueError) as exc:
            stdin_detail = f"write failed: {exc}"
    record("stdin is writable without killing the child (no 2FA exchange attempted)", stdin_ok, stdin_detail)

    print()
    print("=== captured launcher output (stdout+stderr) ===")
    final = b"".join(chunks)
    text = final.decode("utf-8", errors="replace")
    print(text if text.strip() else "<empty>")
    print("=== end captured launcher output ===")
    print()

    # --- teardown: SIGTERM the way a supervisor would, then SIGKILL --------
    # Signal the launcher pid only, never killpg: wrapper-lite-rootless.c:24
    # forwards the signal to its chrooted child, and because the launcher
    # unshares CLONE_NEWPID the payload `lite` is PID 1 of a nested PID
    # namespace, where a signal aimed at "the process group" is not what the
    # supervisor means to express.
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=10)
            record("SIGTERM shuts the child down", True, f"sent SIGTERM, returncode={returncode}")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            record("SIGTERM shuts the child down", False, "sent SIGTERM but needed SIGKILL")
    else:
        # The child was already gone, so no signal was sent and nothing was
        # demonstrated.  This must NOT pass: a check named after an action that
        # was never taken is a vacuous assertion, and this is precisely the code
        # path that section 6.5 of the finding calls "indistinguishable from an
        # external kill" -- a launcher that self-exits 0 after the banner would
        # otherwise be credited with a clean SIGTERM shutdown.
        rc = proc.poll()
        record(
            "SIGTERM shuts the child down",
            False,
            f"NOT TESTED: the child had already exited (returncode={rc}) "
            "before any signal was sent",
        )

    if not args.keep_base_dir:
        shutil.rmtree(base_dir_abs, ignore_errors=True)
    print(f"[INFO] cleaned up {base_dir_abs} (exists={os.path.exists(base_dir_abs)})")

    print()
    print("=== contract summary ===")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    # Every recorded check gates the verdict, so `works` can never contradict a
    # FAIL printed above.  A banner alone is not enough -- see can_bind() for
    # the EADDRINUSE trap.
    works = all(ok for _, ok, _ in results)
    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"VERDICT: {'works' if works else 'needs-fallback'}")
    if not works:
        print(f"FAILED CHECKS ({len(failed)}/{len(results)}): " + "; ".join(failed))
        print()
        print("Read these before concluding the topology is wrong. A genuine")
        print("topology finding is a namespace refusal from the launcher itself")
        print("('unshare:', 'mount proc failed', ...). Everything else is the")
        print("harness: the probe was PID 1, the port was already bound, or the")
        print("rootfs was not writable by the uid the container runs as.")
    return 0 if works else 1


if __name__ == "__main__":
    sys.exit(main())
