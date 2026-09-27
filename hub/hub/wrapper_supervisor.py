"""Supervise `wrapper-lite-rootless`: run it, log in to it, and shut it down.

The hub runs the wrapper itself rather than talking to a wrapper somebody else started, for
one reason: **2FA**. Upstream's container entrypoint reads the 2FA code out of a file on
the host, which a web form cannot fill in. Driven from Python, the hub can see the wrapper's
console output, recognise the moment it asks for a code, and produce that file.

That is the whole design, and it is narrower than it first looks. `wrapper/lite/auth.cpp`
is explicit about what it accepts:

- **Credentials arrive on argv**, as `--login user:pass` (`lite_main.cpp:516-518`, the only
  `set_credentials` call site). Nothing reads them from stdin.
- **The 2FA code arrives in a file**, `<base-dir>/2fa.txt` (`auth.cpp:62`, `78`), and only
  when `--code-from-file` is set or stdin is not a tty. The interactive stdin branch
  (`auth.cpp:64`) is gated on `isatty(STDIN_FILENO)`, and a child this supervisor spawns
  has a pipe there, so **that branch is unreachable from here** and the file is the only
  path that exists. The child polls for it `20 x sleep(3)` and then gives up
  (`auth.cpp:83-88`), and `remove()`s the file itself once read (`auth.cpp:99`).

So `login()` runs a **separate, short-lived login child** -- the launcher is invoked again
in its `--login` mode -- and `submit_2fa()` creates the file that child is waiting for.
That is upstream's own flow: `wrapper/gui/main.go`'s `performLogin` builds exactly this
argv (`--login user:pass --code-from-file --base-dir …`) and watches the same output for
the same markers.

**This class is specific to the two rootfs launchers.** The `<base-dir>/2fa.txt` the
supervisor writes has to land on a path the child can see, which means reproducing the
launcher's `chdir("./rootfs")` + `chroot(".")` (`wrapper-lite-rootless.c:131-138`) on the
host. That mapping is correct for `wrapper-lite-rootless` and for `wrapper-lite`
(`wrapper-lite.c:83-88`, the same `chroot`), and it **cannot work for
`wrapper-lite-qemu`**: that launcher has no `rootfs` at all (0 references, against 10 and
7) and passes `--base-dir /data` *into the guest*, where the file would live in a
namespace the host cannot see. A QEMU deployment therefore has to log in wrapper-side, and
`submit_2fa` refuses with a message saying so rather than writing a file nobody will read.
Note that `config.py`'s `DEFAULT_WRAPPER_BINARY` is still the QEMU launcher; the image's
`AMD_WRAPPER_BINARY` is the rootless one, so that default is what needs to change, not
this module.

**Credentials on argv are visible in `/proc/<pid>/cmdline` to any process of the same uid,
for as long as the child lives.** That is a real exposure and the design does not get
around it; the payload takes no other input. What bounds it here is the topology: the
container runs a single service process as one uid, so the readers of that
`cmdline` are the hub itself and whatever else is in that one container, and the login
child's lifetime is seconds. It is *not* protection on a multi-tenant host. The
mitigations that are in place are that the credentials never reach `log_sink` (every line
the pump forwards is scrubbed, see `_scrub`), never reach the hub's own argv or its
environment, and never reach the database.

Four behaviours are load-bearing, and all four were measured against the real launcher
rather than designed:

- **R6 -- readiness is `GET /status`, never log text.** The launcher prints its banner
  before it serves, and the measured gap was 5.9-18.7 s, so a log-based gate reports ready
  up to nineteen seconds early and the first download fails. Nothing in `start()` reads the
  child's output to decide readiness; the output is only *reported* when readiness fails.
  `startup_timeout` defaults to 60 s against that measured range. Ready means more than a
  200 though: the payload reports `regions: []` until an account is logged in, and an
  instance with no regions cannot download, so readiness also requires a non-empty
  `regions` -- the same test `AppleMusicDecrypt/src/cmd.py:132` makes. **The two ways of
  failing that are reported separately**, because they need different things from the user:
  "it never came up" and "it came up and there is no account on it".
- **R7 -- `stop()` signals the launcher pid, never the process group.** The launcher
  `unshare`s `CLONE_NEWPID`, so the payload `lite` is PID 1 of a nested PID namespace, and
  a `killpg` from outside does not mean what it looks like it means. The launcher forwards
  SIGTERM to its chrooted child (`wrapper-lite-rootless.c:24`) and `lite` consumes it via
  `sigwait` (`lite_main.cpp:447`); the launcher was observed to return `returncode=0`.
- **R8 -- adopt a healthy wrapper that is already on the port.** This host already runs
  one on 127.0.0.1:12340, and the user's own `AppleMusicDecrypt/config.toml` points there,
  so local development collides constantly. Adoption is also the only safe answer, because
  the payload's listening socket takes `SO_REUSEPORT` and never `SO_REUSEADDR`: a second
  launcher on the same port would *share* it, and `/status` could then be answered by
  either process.
- **A port pre-flight, because a bind failure is not reportable from the child's side.**
  `svr.listen()` returning at once makes `lite_main.cpp:705` signal *itself*, so the log
  says `received signal 15, stopping service` -- a line that reads exactly like an external
  kill. `start()` therefore refuses before spawning when the port is held by
  something that does not answer `/status`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# How often readiness is re-probed. 0.2 s is well under the 0.5 s a /key request can take
# and far above the cost of a loopback GET, and it bounds how long a death goes unnoticed.
POLL_INTERVAL = 0.2

# Readiness probes and the pre-flight are short-timeout on purpose. `startup_timeout`
# governs how long the wrapper gets to come up; a hung probe must not eat that budget.
PROBE_TIMEOUT = 2.0

# A connect() to a closed loopback port is refused immediately, so this is only ever
# reached by something that is actually holding the port.
CONNECT_TIMEOUT = 1.0

# How long a login child is given to reach its 2FA prompt. A prompt follows the credential
# handoff within seconds when it is coming at all, and the login child is a separate process
# that exits on its own, so this only bounds how long we wait to notice.
LOGIN_PROMPT_TIMEOUT = 30.0

# How long SIGTERM is given before SIGKILL. The graceful path is the launcher's handler
# forwarding to a chrooted child shutting down an HTTP server, which the spike measured as
# immediate; 15 s is generous for a machine under load and still bounded.
STOP_TIMEOUT = 15.0

# Exponential backoff between automatic restarts, and its ceiling. Restarts are capped and an
# unbounded loop is forbidden, so this only has to be short enough that a genuinely dead
# launcher is reported promptly.
RESTART_BACKOFF_BASE = 0.5
RESTART_BACKOFF_CAP = 8.0

# Lines of the child's own output quoted back in an error. Enough to include the
# namespace-setup `perror` that is usually the whole story and the listen
# banner, without pasting a screenful into an exception.
LOG_TAIL_LINES = 12

# How much the log pump asks the pipe for at a time. A pipe read returns whatever is
# buffered, so this is an upper bound on a burst rather than a wait -- which is what lets a
# log line arrive promptly enough to trigger a challenge.
READ_CHUNK = 4096

# The reader's own limit. 1 MiB is far past any real log line and keeps the 64 KiB default
# from being one pathological line away from an error.
READ_LIMIT = 1 << 20

REDACTED = "***"

# The file `auth.cpp:62` reads the 2FA code from, named once because the supervisor has to
# create it and the child has to remove it.
TWOFA_FILENAME = "2fa.txt"

# The child's own wait for that file: `while (!file_exists && count < 20) sleep(3)` at
# `auth.cpp:84-88`, then `exit(1)`. A challenge may never promise more than this, whatever
# `twofa_ttl` is set to -- a longer TTL would tell a user they have time the wrapper does not
# have.
CHILD_TWOFA_WINDOW = 60.0

# The 2FA prompt heuristic, copied from `wrapper/gui/main.go:653-657` (`check2FA`) rather
# than reinvented. These are the only five strings the upstream GUI treats as a 2FA request,
# and three come from `wrapper/lite/auth.cpp`'s own output: the `credentialHandler` line
# (`2FA: true`) and the file hint that follows it.
TWOFA_MARKERS: tuple[str, ...] = (
    "need2FA: true",
    "2FA: true",
    "2FA code",
    "requiresHSA2VerificationCode",
    "Enter your 2FA code into",
)

# `wrapper-lite-rootless.c` reports every namespace failure as a bare `perror()` and exits.
# These are the strings, so a start-up failure can say which one it was instead of leaving
# the operator to guess from "Operation not permitted".
NAMESPACE_FAILURES: tuple[tuple[str, str], ...] = (
    (
        "unshare:",
        (
            "the kernel refused to create the namespaces; the container needs "
            "security_opt: [seccomp:unconfined, systempaths=unconfined]"
        ),
    ),
    (
        "mount proc failed",
        (
            "mounting a fresh procfs inside the user namespace was refused; that is "
            "the /proc over-mount that systempaths=unconfined removes"
        ),
    ),
    ("mount /dev/urandom failed", "the random device could not be bound into the chroot"),
    (
        "mkdir ./rootfs",
        (
            "the launcher could not create its own rootfs entries, which is what a "
            "uid-mismatched bind mount looks like"
        ),
    ),
    ("open ./rootfs", "the rootfs is not readable by the mapped uid"),
    ("chroot", "the chroot into ./rootfs was refused"),
    (
        "execve",
        (
            "the payload could not be exec'd; usually a library mismatch, e.g. a host "
            "libcurl.so picked up at build time"
        ),
    ),
    ("uid_map", "the user-namespace id map could not be written"),
)

# The EADDRINUSE self-signal, which is the one child log line that looks like something
# else entirely.
SELF_SIGNAL = "received signal 15"


class SupervisorError(RuntimeError):
    """Anything the caller of a supervisor is expected to show a user.

    Every message here is written to be shown: it names the URL, the pid, or the launcher's
    own last words, because the failure modes of this child are not visible from the
    parent's side.
    """


class _ChildExited(SupervisorError):
    """Internal: the child died, so the attempt is worth repeating.

    A private subclass rather than a flag on `SupervisorError`, so the public type stays
    exactly what the rest of the hub catches, and so only a *death* is ever retried -- see
    `start()` and `_restart_after_crash`.
    """


@dataclass(frozen=True)
class LoginChallenge:
    """A pending 2FA code, identified for `submit_2fa` and expiring at `twofa_ttl`.

    `expires_at` is a `time.time()` epoch, not a monotonic reading: it is shown to a user
    ("this code is stale, log in again") and compared against the wall clock in
    `submit_2fa`, so it has to mean the same thing in both places.

    The default TTL is the child's own window, not a round number: `auth.cpp:83-88` polls
    `20 x sleep(3)` and then aborts the login, so a code offered after that cannot be read
    however promptly the user types it. A longer TTL here would promise a deadline the
    wrapper does not honour.
    """

    id: str
    expires_at: float


@dataclass
class _Child:
    """One launcher process we started, its log pump, and the tail of what it said.

    Two of these can exist at once -- the serving child and the short-lived login child --
    and they are the same kind of thing, so they get one type rather than two parallel sets
    of attributes that drift apart.
    """

    proc: asyncio.subprocess.Process
    label: str
    pump: asyncio.Task | None = None
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES))
    partial: str = ""
    twofa_seen: bool = False
    # When the prompt marker was seen, not when the challenge was minted. The child starts
    # its own 60 s window at the marker, so a deadline measured from later than this is
    # later than the child's and offers a user a code nobody will read.
    twofa_seen_at: float | None = None
    twofa_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


@dataclass(frozen=True)
class _Probe:
    """One `GET /status` attempt, kept whole so the failure message can be precise.

    `answered` and `usable` are separate because they answer different questions: "did
    anything speak HTTP on this port" and "is what it said a wrapper status". The difference
    is what separates "still starting up" from "not a wrapper at all".
    """

    answered: bool
    usable: bool
    regions: list
    data: dict
    detail: str


class WrapperSupervisor:
    """Start, watch, log in to, and stop one `wrapper-lite-rootless`.

    Not a singleton and not thread-safe: one instance per wrapper, driven from one event
    loop. `creart` in `ripper_host.py` is what decides who owns it.
    """

    def __init__(
        self,
        *,
        binary: Path,
        base_dir: Path,
        host: str,
        port: int,
        log_sink: Callable[[str], None],
        twofa_ttl: float = 60.0,
        max_restarts: int = 3,
        startup_timeout: float = 60.0,
        adopt_existing: bool = True,
    ) -> None:
        self._binary = Path(binary)
        self._base_dir = Path(base_dir)
        self._host = host
        self._port = port
        self._log_sink = log_sink
        self._twofa_ttl = twofa_ttl
        self._max_restarts = max_restarts
        self._startup_timeout = startup_timeout
        self._adopt_existing = adopt_existing

        self._service: _Child | None = None
        self._login: _Child | None = None
        self._adopted = False
        self._bound_port = 0
        self._client: httpx.AsyncClient | None = None
        self._watch: asyncio.Task | None = None
        self._spawn_lock = asyncio.Lock()
        self._stopping = False
        # Automatic restarts spent since the last `start()`, shared by the start-up retry
        # loop and the crash watcher. One budget for one epoch, so the total number of
        # spawns the supervisor will make without being asked is bounded.
        self._restarts_used = 0

        # Everything that must never reach a log pane, scrubbed on the way out. A set
        # because the order does not matter here -- `_scrub` sorts by length.
        self._secrets: set[str] = set()
        # id -> expiry epoch. Survives `stop()` on purpose: a code offered to a login child
        # that has since exited has to be refused for the reason it failed, which is "the
        # wrapper is not running", not "no such challenge".
        self._challenges: dict[str, float] = {}

    # -- observable state ---------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a wrapper is available to serve requests right now.

        True for an adopted instance too: the question a caller asks is "can I use a
        wrapper", and the answer is yes whether we started it or not. `adopted` is how a
        caller tells the two apart -- the UI has to, because an adopted one cannot be logged
        into.
        """
        return self._adopted or (self._service is not None and self._service.alive)

    @property
    def adopted(self) -> bool:
        """True when this supervisor drives a wrapper it did not start (R8)."""
        return self._adopted

    @property
    def pid(self) -> None | int:
        """The serving launcher's pid, or None when there is no live child to signal.

        None rather than a dead pid because the only use for it is signalling, and R7 makes
        that the sharpest edge in this module.
        """
        if self._service is None or not self._service.alive:
            return None
        return self._service.proc.pid

    @property
    def bound_port(self) -> int:
        """The port the wrapper is actually on.

        Differs from the requested `port` whenever `port=0`, which means "bind an ephemeral
        port": the supervisor picks one, because learning it from the child's log output
        would be exactly the log-text gate R6 forbids. 0 before the first successful start.
        """
        return self._bound_port

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Bring the wrapper up, or raise `SupervisorError` saying why not.

        Adopts an existing healthy wrapper when `adopt_existing` is set, otherwise spawns.
        A failure always leaves no child behind: the point of `start()` raising is that the
        UI can offer a retry, and a silently surviving half-started wrapper would hold the
        port against that retry.

        Retries, both here and in the crash watcher, are for a child that *died*, at most
        `max_restarts` times with exponential backoff, and never for anything else. In
        particular a readiness **timeout is not retried**: the child is alive and may simply
        be one of the 19-second startups the spike measured, and respawning it would turn a
        slow start into an infinite one. An occupied port or a missing binary is reported
        immediately, because retrying either changes nothing.
        """
        if self.running:
            return
        self._restarts_used = 0

        for attempt in range(1, self._max_restarts + 2):
            await self._ensure_child()
            try:
                await self._wait_ready()
            except _ChildExited as died:
                if attempt > self._max_restarts:
                    message = str(died)
                    await self._shutdown_service()
                    raise SupervisorError(message) from None
                self._restarts_used = attempt
                self._emit(
                    f"the wrapper exited while starting up; restarting in "
                    f"{self._backoff(attempt):.1f}s (attempt {attempt} of "
                    f"{self._max_restarts + 1})"
                )
                await asyncio.sleep(self._backoff(attempt))
                continue
            except SupervisorError as not_ready:
                # Not retried, by the reasoning in the docstring. Torn down rather than
                # left half-alive, so the caller can retry cleanly.
                await self._shutdown_service()
                # Named re-raise on purpose: the failure is re-thrown only after the child
                # has been torn down.
                raise not_ready  # noqa: TRY201

            self._stopping = False
            self._emit(
                f"the wrapper is ready on {self._status_url()} "
                f"(pid {self.pid}, port {self._bound_port})"
            )
            self._start_watching()
            return

    async def stop(self) -> None:
        """Shut down what this supervisor started, and nothing else. Idempotent.

        An adopted wrapper is left running: it is somebody else's process, and on this host
        it is a service the user started by hand. Both of our children -- the serving one
        and any login child -- get SIGTERM on their own pid and are reaped before this
        returns, so a caller that checks `os.kill(pid, 0)` afterwards is not looking at a
        zombie. The HTTP client is closed.
        """
        self._stopping = True
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
            self._watch = None
        await self._shutdown_service()
        await self._shutdown_login()
        self._adopted = False
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def status(self) -> dict:
        """The `data` object of `GET /status`, e.g. `{"regions": ["jp"]}`.

        Uncached, unlike the downloader's client: the hub re-reads it to drive the
        "regions went empty" path, which is a state change a cache would hide.
        """
        if self._bound_port == 0:
            raise SupervisorError(
                "the supervisor is not started yet, so there is no wrapper to ask"
            )
        if not self._adopted and (self._service is None or not self._service.alive):
            # Deliberately not keyed on `_bound_port`, which `_pick_port()` sets *before* the
            # spawn: after a failed `start()` the port is known and nothing is listening, and
            # the caller would get a ConnectError phrasing for what is really "it is not
            # running".
            raise SupervisorError(
                f"no wrapper is running to ask, so there is nothing at "
                f"{self._status_url()}. The last attempt at pid "
                f"{self._service.proc.pid if self._service else 'none'} exited with code "
                f"{self._service.proc.returncode if self._service else 'n/a'}; start it "
                f"again."
            )
        probe = await self._probe_status(self._bound_port)
        if not probe.usable:
            raise SupervisorError(
                f"{self._status_url()} is not answering with a usable status: "
                f"{probe.detail}"
            )
        return probe.data

    # -- login --------------------------------------------------------------

    async def login(self, username: str, password: str) -> LoginChallenge:
        """Log in to Apple Music and wait for the wrapper to ask for a 2FA code.

        Runs the launcher a second time, in its `--login` mode, with the credentials on its
        argv -- the only input it takes (`lite_main.cpp:516-518`). It is a separate, short
        lived process from the serving one: the payload's login mode caches tokens and
        `return 0`, it never listens (`lite_main.cpp:538-600`). So this does not need the
        serving child to be up, which is what makes it usable at all: `start()` waits for
        `regions`, and `regions` stay empty until an account is logged in.

        Returns the pending challenge once the child asks for a code. Raises
        `SupervisorError` if it never asks within `LOGIN_PROMPT_TIMEOUT` -- either the
        account needs no 2FA (the login is done; check `/status`) or the credentials were
        rejected (the child's own output says which). There is no non-2FA return value to
        give, because there is no code left to submit.

        A serving child started before this point will not pick the new account up on its
        own: the payload loads its token cache at start-up (`lite_main.cpp:614`). Call
        `stop()` and `start()` again after a successful login.
        """
        if self._adopted:
            raise SupervisorError(
                "this hub adopted a wrapper that was already running, and an adopted "
                "wrapper cannot be logged into from here -- its login happens in its own "
                "process, not this one. Log in on the wrapper side (for example "
                "`lite --login user:pass`) and retry."
            )

        await self._shutdown_login()  # a second login supersedes the first
        # Outstanding challenges are dropped with it, and deliberately. Their login child is
        # gone, and the new one watches the *same* `2fa.txt`, so a code offered against a
        # stale challenge would be picked up by the new login and appended to the wrong
        # account's password (`auth.cpp:92-96` concatenates code onto the password).
        if self._challenges:
            self._emit(
                f"discarding {len(self._challenges)} outstanding 2FA challenge(s) from "
                f"the previous login attempt"
            )
            self._challenges.clear()
        # A leftover 2fa.txt is removed here, before anything is spawned, because this is the
        # one moment a file at that path is unambiguously not ours: a previous login that
        # was stopped rather than completed cannot remove it (the child only unlinks at
        # `auth.cpp:99`, which SIGTERM prevents), and the wait at `auth.cpp:84` is guarded
        # by `file_exists` while the `fopen`/`fscanf` that follows is *not* -- so a stale
        # file is appended to the new password the instant it appears, with no prompt and
        # no warning.
        self._discard_twofa_file("before starting a new login")
        self._remember_secret(username)
        self._remember_secret(password)
        # The joined form too: it is exactly what lands in argv and in
        # /proc/<pid>/cmdline, and it is what a child that echoes its own argv would print.
        self._remember_secret(f"{username}:{password}")

        argv = [
            str(self._binary),
            "--login",
            f"{username}:{password}",
            # Takes the file path unconditionally, rather than leaving it to the isatty
            # test that a piped stdin can never pass (`auth.cpp:64`). Same flag, same
            # reason, as the Go GUI's argv (`main.go:733`).
            "--code-from-file",
            "--base-dir",
            str(self._base_dir),
        ]
        # Emitted without the credentials even though they are in argv: this line goes to
        # the UI, and the scrub is a second line of defence rather than the only one.
        self._emit(
            f"spawning {self._binary} in login mode with --code-from-file "
            f"(--base-dir {self._base_dir})"
        )

        try:
            child = await self._spawn(argv, label="login")
        except OSError as exc:
            raise SupervisorError(
                f"could not start the wrapper launcher {self._binary} to log in: {exc!r}"
            ) from exc
        self._login = child

        # Wait for whichever comes first: the 2FA prompt, or the child exiting. The login
        # mode exits by itself when it is done (`lite_main.cpp:596-600`), so an account
        # that needs no 2FA is the *common* outcome and must not cost the full prompt
        # timeout before the caller hears about it.
        prompted = asyncio.ensure_future(child.twofa_event.wait())
        exited = asyncio.ensure_future(child.proc.wait())
        try:
            await asyncio.wait(
                {prompted, exited},
                timeout=LOGIN_PROMPT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in (prompted, exited):
                waiter.cancel()

        if not child.twofa_seen:
            await self._drain_pump(child)
            detail = self._tail_text(child)
            await self._shutdown_login()
            # Only worth pointing at a URL when there is one: on a fresh install nothing
            # is serving, and "check http://127.0.0.1:0/status" would be nonsense.
            check = (
                f"check {self._status_url()}"
                if self._bound_port
                else "start the wrapper and check its /status"
            )
            if child.proc.returncode is None:
                raise SupervisorError(
                    f"the wrapper did not ask for a 2FA code within "
                    f"{LOGIN_PROMPT_TIMEOUT:.0f}s. If this account needs no 2FA the login is "
                    f"done -- {check}. Otherwise the credentials were rejected, and the "
                    f"wrapper's own output says why:\n{detail}"
                )
            raise SupervisorError(
                f"the login process finished without asking for a 2FA code (exit code "
                f"{child.proc.returncode}). If this account needs no 2FA the login is done "
                f"-- {check}. Otherwise it was rejected, and the wrapper's own output says "
                f"why:\n{detail}"
            )

        # The deadline is the child's, measured from the child's own moment. Minting it from
        # `time.time()` here would start the clock after the marker, so the supervisor's
        # window would end slightly *later* than the payload's `exit(1)` -- and it is exactly
        # in that sliver that `submit_2fa` can write a file no one will read. `twofa_ttl` is
        # clamped to `CHILD_TWOFA_WINDOW` so no configuration can promise more time than the
        # wrapper has.
        window = min(self._twofa_ttl, CHILD_TWOFA_WINDOW)
        seen_at = child.twofa_seen_at if child.twofa_seen_at is not None else time.time()
        challenge = LoginChallenge(id=uuid.uuid4().hex, expires_at=seen_at + window)
        self._challenges[challenge.id] = challenge.expires_at
        remaining = max(0.0, challenge.expires_at - time.time())
        self._emit(
            f"the wrapper asked for a 2FA code; enter it within {remaining:.0f}s, after "
            f"which the wrapper gives up and exits"
        )
        return challenge

    async def submit_2fa(self, challenge_id: str, code: str) -> None:
        """Create `<base-dir>/2fa.txt` for the waiting login child, consuming the challenge.

        A file, not a write to the child's stdin: `auth.cpp:64`'s stdin branch needs a tty
        and a spawned child has a pipe there, so the file is the only channel the payload
        offers. The child polls for it and `remove()`s it itself (`auth.cpp:99`) -- but only
        if it gets that far, so `login()` and `_shutdown_login()` also remove it. See
        `_discard_twofa_file` for why that is not optional.

        Single use and TTL-checked: an expired challenge is re-requested, not resent.
        The code joins the redaction set before the file is written, because the
        child echoes what it read back onto its stdout.
        """
        if self._adopted:
            raise SupervisorError(
                "this hub adopted a wrapper that was already running, so there is no login "
                "child here to hand a 2FA code to"
            )

        expires_at = self._challenges.get(challenge_id)
        if expires_at is None:
            raise SupervisorError(
                f"unknown or already-used 2FA challenge {challenge_id!r}; log in again for "
                f"a new one"
            )
        if time.time() > expires_at:
            self._challenges.pop(challenge_id, None)
            raise SupervisorError(
                f"the 2FA challenge {challenge_id} expired {time.time() - expires_at:.0f}s "
                f"ago, around when the wrapper stopped waiting for the code; log in again "
                f"for a new one"
            )
        if self._login is None or not self._login.alive:
            raise SupervisorError(
                f"the wrapper's login process is not running, so the 2FA code cannot be "
                f"delivered; it exited with code "
                f"{'nothing, it was never started' if self._login is None else self._login.proc.returncode}"
            )

        self._challenges.pop(challenge_id, None)
        self._remember_secret(code)
        path = self._write_twofa_file(code)
        self._emit(
            f"2FA code written to {path}; the wrapper picks it up and removes the file"
        )

    # -- the 2FA file -------------------------------------------------------

    def _twofa_file(self) -> Path:
        """Where the login child will look for the code, as a path on *this* side.

        `auth.cpp:62` builds the path from `g_base_dir`, which the launcher resolves *after*
        `chdir("./rootfs")` + `chroot(".")` (`wrapper-lite-rootless.c:131-138`) and then
        `mkdir`s inside that tree (`:142`) -- so on the host it lives under the `rootfs`
        directory next to the binary, whatever the configured base dir says. Verified against
        the real launcher: `--base-dir X` appears as `rootfs/X`.

        An absolute `--base-dir` is still chroot-absolute, so the leading separator is
        dropped rather than treated as a host root.
        """
        chroot = self._binary.parent / "rootfs"
        if not chroot.is_dir():
            raise SupervisorError(
                f"{chroot} is not a directory, so there is nowhere to put the 2FA file. "
                f"The launcher chroots into ./rootfs relative to its own directory "
                f"(wrapper-lite-rootless.c:131-138) and reads the code from "
                f"<base-dir>/2fa.txt *inside* that tree, so the hub can only hand it a code "
                f"for a rootfs launcher: wrapper-lite-rootless or wrapper-lite. The QEMU "
                f"launcher (wrapper-lite-qemu) has no rootfs at all -- it passes --base-dir "
                f"into the guest, where the file would be invisible from here."
            )
        relative = (
            self._base_dir.relative_to("/")
            if self._base_dir.is_absolute()
            else self._base_dir
        )
        return chroot / relative / TWOFA_FILENAME

    def _discard_twofa_file(self, why: str) -> bool:
        """Remove the 2FA file if it is there. True if one was removed.

        This is not tidiness. `auth.cpp:84` guards only the *wait* on `file_exists`; the
        `fopen` + `fscanf` at `:92-96`, which appends whatever the file holds to the
        password, is unconditional. So a leftover file is not a file nobody reads -- the next
        login consumes it instantly, silently, and authenticates with the wrong password
        appended to it. A login that is stopped rather than completed always leaves one,
        because the child only unlinks at `:99` and SIGTERM prevents it from getting there.

        Called from two places, and both are needed: before a new login starts, where a
        file at that path cannot be ours, and after a login child we signalled, where the
        file is ours and orphaned.
        """
        try:
            path = self._twofa_file()
        except SupervisorError:
            # No rootfs: there is no file to have been left, and `_twofa_file` has already
            # explained itself where it matters.
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._emit(f"could not remove the leftover 2FA file {path}: {exc!r}")
            return False
        self._emit(f"removed a leftover 2FA file {path} {why}")
        return True

    def _write_twofa_file(self, code: str) -> Path:
        """Create the 2FA file atomically, owner-only, and return its path.

        Written to a temporary name and `os.replace`d into place because the child polls
        with `file_exists` and then `fopen`s: a partially written file would be read as a
        truncated code rather than as no code at all. Mode 0600 because the launcher creates
        its base dir 0777 (`wrapper-lite-rootless.c:142`), so the file's own mode is the
        only thing keeping the code off other readers.

        `O_EXCL`, and the temporary name is unlinked first: without both, a `.2fa.txt.<pid>`
        left behind by an earlier run of *this* process keeps whatever mode it had, so the
        0600 that `O_CREAT` requests is silently not applied.
        """
        path = self._twofa_file()
        if not path.parent.is_dir():
            raise SupervisorError(
                f"{path.parent} does not exist, so the 2FA code cannot be handed over. "
                f"The wrapper's login process creates it; it may have exited already."
            )
        temporary = path.with_name(f".{TWOFA_FILENAME}.{os.getpid()}")
        with contextlib.suppress(OSError):
            temporary.unlink()
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, code.encode())
            finally:
                os.close(fd)
            os.replace(temporary, path)
        except OSError as exc:
            # Covers a crash between `open` and `replace` too, not just a failed open: the
            # temporary is unlinked on every path out of this block.
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise SupervisorError(
                f"could not write the 2FA code to {path}: {exc!r}"
            ) from exc
        return path

    # -- starting -----------------------------------------------------------

    async def _ensure_child(self) -> None:
        """Make sure a live serving child exists, spawning one if it does not.

        Under a lock because `start()` and the crash watcher can both get here, and a
        double spawn would put two launchers on one port -- which `SO_REUSEPORT` would
        allow, and which would make `/status` untrustworthy.
        """
        async with self._spawn_lock:
            if self._service is not None and self._service.alive:
                return
            await self._reap(self._service)
            self._service = None
            self._stopping = False
            await self._spawn_service()

    async def _spawn_service(self) -> None:
        if await self._preflight():
            return

        self._bound_port = self._pick_port()
        argv = [
            str(self._binary),
            "--base-dir",
            str(self._base_dir),
            "--host",
            self._host,
            "--port",
            str(self._bound_port),
        ]
        # Logged with the port and nothing else: this line goes to the UI, and a serving
        # child never carries credentials.
        self._emit(f"spawning {argv[0]} on {self._host}:{self._bound_port}")
        self._service = await self._spawn(argv, label="service")

    async def _spawn(self, argv: list[str], *, label: str) -> _Child:
        """Create one launcher process and start reading its output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                # `cwd` is the binary's directory, never the inherited one:
                # `wrapper-lite-rootless.c` chroots into `./rootfs` and resolves
                # `--base-dir` *after* `chroot(".")`, both relative to the CWD.
                cwd=str(self._binary.parent),
                # stderr folded into stdout: every `LOG_*` line goes to stderr
                # (`wrapper/lite/logger.h:59`), unbuffered, and the two must be read as one
                # ordered stream to see a prompt where it appears.
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # DEVNULL, not a pipe. There is nothing to write here: the credentials are
                # on argv and the code is in a file, so the only thing stdin has to be is
                # something the payload's isatty test fails -- which is the point of it.
                stdin=asyncio.subprocess.DEVNULL,
                limit=READ_LIMIT,
            )
        except OSError as exc:
            raise SupervisorError(
                f"could not start the wrapper launcher {self._binary}: {exc!r}. The "
                f"launcher must exist and be executable, and its parent directory must "
                f"contain the rootfs it chroots into."
            ) from exc
        child = _Child(proc=proc, label=label)
        child.pump = asyncio.create_task(
            self._pump_output(child), name=f"wrapper-lite-log-pump-{label}"
        )
        return child

    async def _preflight(self) -> bool:
        """Decide between adopting, refusing, and spawning. True means "adopted".

        Only meaningful for an explicit port. `port=0` means "pick one for me", so there is
        nothing to inspect and nothing to collide with.
        """
        if self._port == 0:
            return False

        probe = await self._probe_status(self._port)
        if probe.usable:
            if not self._adopt_existing:
                raise SupervisorError(
                    f"port {self._port} on {self._host} is already served by a wrapper "
                    f"answering /status, and adoption is switched off; point "
                    f"AMD_WRAPPER_PORT somewhere else or enable adoption"
                )
            self._adopted = True
            self._bound_port = self._port
            self._emit(
                f"adopted the wrapper already serving on {self._status_url()}; logins have "
                f"to happen on the wrapper side"
            )
            return True

        if await self._port_has_listener(self._port):
            raise SupervisorError(
                f"port {self._port} on {self._host} is in use by another process, which "
                f"does not answer /status ({probe.detail}). Refusing to start a launcher "
                f"there: it would fail its bind with EADDRINUSE and log "
                f"'{SELF_SIGNAL}', which is indistinguishable from an external kill."
            )
        return False

    async def _port_has_listener(self, port: int) -> bool:
        """Whether something accepts a connection on `port`.

        A refused connection means nothing is there, *including* a TIME_WAIT remnant: those
        have no listener, and the payload binds with `SO_REUSEPORT` so a remnant does not
        block it. A connect that times out is counted as occupied, because on loopback a
        connect either succeeds or is refused at once.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, port), CONNECT_TIMEOUT
            )
        except TimeoutError:
            return True
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    def _pick_port(self) -> int:
        if self._port != 0:
            return self._port
        # Bind-and-release, exactly as the spike probe picked its port. Inherently racy for
        # the microseconds between the close and the child's bind, and harmless: the
        # payload's `SO_REUSEPORT` means a lost race is a shared port rather than a failure,
        # and the readiness probe would then be answered by the winner.
        with socket.socket() as sock:
            sock.bind((self._host, 0))
            return int(sock.getsockname()[1])

    # -- readiness ----------------------------------------------------------

    async def _wait_ready(self) -> None:
        """Poll `/status` until it reports regions, or give up. R6, in one place.

        Nothing here reads the child's output to decide readiness; the only thing the
        output is used for is being reported when this fails.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        last_detail = "no probe was completed"
        serving_without_regions = False

        while True:
            await self._raise_if_dead()
            probe = await self._probe_status(self._bound_port)
            last_detail = probe.detail
            if probe.usable and probe.regions:
                return
            if probe.usable and not serving_without_regions:
                serving_without_regions = True
                # Said once, not every poll: the payload serves this state for as long as no
                # account is logged in, which is the state a fresh install boots into.
                self._emit(
                    f"the wrapper is serving on {self._status_url()} but reports no "
                    f"regions: it is up and healthy, there is just no Apple account logged "
                    f"in on it yet, so the hub should offer to log in"
                )
            await self._raise_if_dead()

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POLL_INTERVAL, remaining))

        if serving_without_regions:
            # The two cases need different things from the user, so they are worded
            # differently. This one is not "come back in a minute": the wrapper is already
            # serving, and the only thing missing is an account.
            message = (
                f"no account is logged in on the wrapper at {self._status_url()}: it is "
                f"up and answering /status, but regions is empty, so it cannot serve a "
                f"download. Log in and start it again. Nothing needs to be waited for -- "
                f"the wrapper itself is ready."
            )
        else:
            message = (
                f"the wrapper did not become ready within {self._startup_timeout:.0f}s: "
                f"{self._status_url()} never returned a usable status envelope (last "
                f"probe: {last_detail}). Its own output was:\n{self._tail_text(self._service)}"
            )
        raise SupervisorError(message)

    async def _raise_if_dead(self) -> None:
        """Turn "the child exited" into a message that says what it printed.

        Draining the pump first is what makes the message useful: the pipe still holds the
        child's last words at the moment it exits, and a `perror` on the way out is usually
        the entire diagnosis.
        """
        child = self._service
        if child is None or child.alive:
            return
        await self._drain_pump(child)
        raise _ChildExited(
            f"the wrapper launcher (pid {child.proc.pid}) exited with code "
            f"{child.proc.returncode} before the wrapper was ready."
            f"{self._diagnose_tail(child)} Its output was:\n{self._tail_text(child)}"
        )

    # -- the crash watcher --------------------------------------------------

    def _start_watching(self) -> None:
        self._watch = asyncio.create_task(
            self._watch_service(), name="wrapper-lite-watch"
        )

    async def _watch_service(self) -> None:
        """Respawn a crashed wrapper with backoff, up to the budget, then give up.

        An unexpected exit is a restart with exponential backoff, three times,
        and after that it is the user's problem rather than a loop. The budget is shared
        with `start()`'s own retry loop (`_restarts_used`), so the supervisor will not
        spawn more than `max_restarts` extra children per epoch however the deaths arrive.

        A restart is only counted as a success once the wrapper is *serving again* -- the
        same `/status` gate `start()` uses. A child that respawns and dies immediately is
        still a failed restart, and spends the budget like one, which is what stops a
        crash-loop from looking like progress.
        """
        child = self._service
        if child is None:
            return
        try:
            await child.proc.wait()
        except asyncio.CancelledError:
            return
        if self._stopping or child is not self._service:
            return
        self._emit(
            f"the wrapper exited unexpectedly with code {child.proc.returncode}; it is no "
            f"longer running.{self._diagnose_tail(child)}"
        )
        await self._restart_after_crash()

    async def _restart_after_crash(self) -> None:
        while not self._stopping:
            if self._restarts_used >= self._max_restarts:
                self._service = None
                self._emit(
                    f"the wrapper will not be restarted: the automatic restart budget is "
                    f"spent ({self._restarts_used} of {self._max_restarts} used) and the "
                    f"last attempt did not come back. Start it again by hand, and expect "
                    f"the same failure until whatever is causing it is fixed."
                )
                return
            attempt = self._restarts_used + 1
            delay = self._backoff(attempt)
            self._emit(
                f"restarting the wrapper in {delay:.1f}s "
                f"(automatic restart {attempt} of {self._max_restarts})"
            )
            await asyncio.sleep(delay)
            if self._stopping:
                return
            self._restarts_used = attempt
            try:
                await self._ensure_child()
                await self._wait_ready()
            except SupervisorError as exc:
                self._emit(f"automatic restart {attempt} did not work: {exc}")
                await self._shutdown_service()
                continue
            self._emit(
                f"the wrapper is serving again on {self._status_url()} "
                f"(pid {self.pid}, port {self._bound_port})"
            )
            self._start_watching()
            return

    def _backoff(self, attempt: int) -> float:
        return min(RESTART_BACKOFF_BASE * 2 ** (attempt - 1), RESTART_BACKOFF_CAP)

    # -- the child's output -------------------------------------------------

    async def _pump_output(self, child: _Child) -> None:
        """Read one child's output, scrub it, and hand it to `log_sink`.

        This task must not die. The child writes to a pipe nobody else drains, so a pump
        that stops turns a working wrapper into a wedged one within one pipe buffer. Every
        failure here is reported into the sink and ends the loop; the one exception
        propagated is `CancelledError`, which is `stop()`'s own business.

        `read()`, not `readline()`: `StreamReader.readline()` waits for a newline, and the
        lines this supervisor acts on are not guaranteed to arrive complete. The Go GUI has
        the same shape -- it sees one `Write` per pipe read and also inspects the raw chunk
        (`main.go:686`). Checking the *accumulated remainder* rather than the raw chunk
        additionally means a marker split across two reads is still found.
        """
        stream = child.proc.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(READ_CHUNK)
                if not chunk:
                    break
                child.partial += chunk.decode("utf-8", "replace")
                while "\n" in child.partial:
                    line, child.partial = child.partial.split("\n", 1)
                    self._deliver(child, line.rstrip("\r"))
                    self._check_2fa(child, line)
                self._check_2fa(child, child.partial)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate: see the docstring
            self._emit(f"the wrapper log pump for {child.label} stopped: {exc!r}")
        finally:
            if child.partial:
                self._deliver(child, child.partial)
                child.partial = ""

    def _deliver(self, child: _Child, line: str) -> None:
        """Scrub one line, keep it for diagnostics, and forward it.

        The redaction happens *here*, on the only path to `log_sink`. It matters most now
        that credentials are on argv: the child can echo its own command line, and a crash
        report that quotes argv would put the password in the UI otherwise.
        """
        scrubbed = self._scrub(line)
        child.tail.append(scrubbed)
        # Blank lines dropped, matching the upstream writer: the log pane is for content.
        if scrubbed.strip():
            self._emit(scrubbed)

    def _scrub(self, text: str) -> str:
        """Replace every known credential value with `REDACTED`.

        Longest first, and that ordering is the whole correctness of this function: a
        username that is a prefix of its own password (`SECRET` / `SECRET_PASS`) would
        otherwise be replaced first and leave `***_PASS` in the log.

        Every value is replaced whatever its length, so a one-character password mangles
        every line that happens to contain that character. That is the safe direction to
        fail in -- a short secret is still a secret -- and a password long enough not to
        collide with ordinary log text costs nothing.
        """
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    def _remember_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def _check_2fa(self, child: _Child, text: str) -> None:
        if child.twofa_seen:
            return
        if any(marker in text for marker in TWOFA_MARKERS):
            child.twofa_seen = True
            # Stamped here rather than when the challenge is minted: this is the moment the
            # child starts counting, so this is the only timestamp the deadline may be
            # measured from.
            child.twofa_seen_at = time.time()
            child.twofa_event.set()

    async def _drain_pump(self, child: _Child, timeout: float = 0.5) -> None:
        """Let the pump finish a child's output, without ever cancelling it.

        `asyncio.wait` rather than `wait_for`, because a timeout here means "stop waiting",
        not "abandon the reader" -- cancelling the pump is how output gets lost.
        """
        if child.pump is None or child.pump.done():
            return
        await asyncio.wait({child.pump}, timeout=timeout)

    def _emit(self, line: str) -> None:
        """One line to `log_sink`, and never an exception out of it.

        The API layer's log sink appends to an HTMX log pane, so it can raise for reasons
        that have nothing to do with the wrapper. Letting that reach the pump would kill the
        reader.
        """
        try:
            self._log_sink(line)
        except Exception:  # noqa: BLE001, S110 - a broken sink must not stop us
            pass

    def _tail_text(self, child: _Child | None) -> str:
        if child is None or not child.tail:
            return "    (the launcher produced no output)"
        return "\n".join(f"    {line}" for line in child.tail)

    def _diagnose_tail(self, child: _Child | None) -> str:
        """Name the launcher's failure from its own `perror` strings, where possible.

        The strings are the launcher's, and they are the difference between "did not become
        ready" and "the kernel refused to create the namespaces, which is what a container
        without `systempaths=unconfined` produces".
        """
        if child is None:
            return ""
        text = "\n".join(child.tail)
        if SELF_SIGNAL in text:
            return (
                f" The payload signalled *itself*, which its own code does when "
                f"`svr.listen()` fails (`lite_main.cpp:705`) -- the usual cause is "
                f"EADDRINUSE on {self._host}:{self._bound_port}."
            )
        for marker, explanation in NAMESPACE_FAILURES:
            if marker in text:
                return f" That looks like {explanation}."
        return ""

    # -- the wrapper's HTTP endpoint ----------------------------------------

    def _status_url(self) -> str:
        return f"http://{self._host}:{self._bound_port}/status"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(PROBE_TIMEOUT),
                # The wrapper is loopback-only and must never be reached through a
                # proxy. httpx honours `HTTP_PROXY`/`ALL_PROXY` by default, and a proxy
                # that answers 200 for anything would fake readiness.
                trust_env=False,
            )
        return self._client

    async def _probe_status(self, port: int) -> _Probe:
        """One `GET /status`, decomposed into the three questions a caller has.

        The envelope handling is the same as `AppleMusicDecrypt/src/wrapper.py`'s
        `_decode_response`, deliberately: the hub and the CLI have to agree on what a
        healthy instance looks like, or the same wrapper reads as up in one and down in the
        other.
        """
        url = f"http://{self._host}:{port}/status"
        try:
            response = await self._http().get(url)
        except httpx.HTTPError as exc:
            return _Probe(False, False, [], {}, f"GET {url} failed: {exc!r}")
        if response.status_code != 200:
            return _Probe(
                True, False, [], {}, f"GET {url} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            return _Probe(True, False, [], {}, f"GET {url} returned a non-JSON body")
        if not isinstance(payload, dict) or "code" not in payload:
            return _Probe(
                True, False, [], {}, f"GET {url} returned an unexpected envelope"
            )
        if payload.get("code") != 0:
            return _Probe(
                True,
                False,
                [],
                {},
                f"GET {url} returned code={payload.get('code')!r} "
                f"msg={payload.get('msg')!r}",
            )
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        regions = data.get("regions")
        return _Probe(
            True, True, regions if isinstance(regions, list) else [], data, "HTTP 200, code=0"
        )

    # -- teardown -----------------------------------------------------------

    async def _reap(self, child: _Child | None) -> None:
        if child is None:
            return
        with contextlib.suppress(Exception):
            await child.proc.wait()
        await self._drain_pump(child)

    async def _terminate(self, child: _Child) -> None:
        """SIGTERM one child on its own pid, then reap it. R7, in one place."""
        if not child.alive:
            await self._reap(child)
            return
        self._emit(f"stopping the {child.label} (pid {child.proc.pid}) with SIGTERM")
        # The pid, never the process group (R7): the launcher `unshare`s `CLONE_NEWPID`, so
        # the payload is PID 1 of a nested PID namespace where a group signal does not mean
        # what it appears to. Signalling the launcher is also sufficient and graceful -- it
        # forwards to its chrooted child (`wrapper-lite-rootless.c:24`) and `lite` stops its
        # server via sigwait.
        with contextlib.suppress(ProcessLookupError):
            child.proc.terminate()
        try:
            await asyncio.wait_for(child.proc.wait(), STOP_TIMEOUT)
        except TimeoutError:
            self._emit(
                f"the {child.label} did not exit within {STOP_TIMEOUT:.0f}s of SIGTERM; "
                f"SIGKILL to pid {child.proc.pid}"
            )
            with contextlib.suppress(ProcessLookupError):
                child.proc.kill()
            await child.proc.wait()
        # After the exit, not before: the child needs someone reading the pipe until it
        # closes, or a full buffer would block its own shutdown.
        await self._drain_pump(child)

    async def _discard_pump(self, child: _Child) -> None:
        if child.pump is None:
            return
        child.pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await child.pump
        child.pump = None

    async def _shutdown_service(self) -> None:
        child, self._service = self._service, None
        if child is None:
            return
        await self._terminate(child)
        await self._discard_pump(child)

    async def _shutdown_login(self) -> None:
        """Tear down the login child. A login child exits on its own when it is done.

        Its own exit is not a failure and is not reported as one: the login mode returns 0
        after caching tokens (`lite_main.cpp:596-600`). Anything else is left to the pump
        and the log, which already carry the payload's own account of what went wrong.

        The 2FA file is removed when the child was still running, i.e. when *we* stopped it.
        That is the case where the child cannot have removed it: `auth.cpp:99` is the last
        thing the login does, and a signal kills it before then. Leaving the file would
        hand the next login a code with no prompt, which `auth.cpp:92-96` appends to
        whatever password that login is using.
        """
        child, self._login = self._login, None
        if child is None:
            return
        we_signalled = child.alive
        await self._terminate(child)
        await self._discard_pump(child)
        if we_signalled:
            self._discard_twofa_file("left behind by a login that was stopped")
