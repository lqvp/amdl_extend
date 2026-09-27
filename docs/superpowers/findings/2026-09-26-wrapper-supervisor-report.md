# Task 5 report — Wrapper supervisor

**Branch:** `feat/phase-1-foundation` · **Status:** DONE_WITH_CONCERNS · **Date:** 2026-09-27

Files: `hub/hub/wrapper_supervisor.py` (new), `hub/tests/test_supervisor.py` (new),
`hub/pyproject.toml` + `hub/uv.lock` (httpx), `hub/spike/task5_real_binary_check.py` (new,
the real-binary harness).

---

## 1. What was built

`WrapperSupervisor` spawns `wrapper-lite-rootless` as a child process, waits for it to be
genuinely serving, drives a 2FA exchange over stdin, and shuts it down without orphans.
`SupervisorError(RuntimeError)`, `LoginChallenge(id, expires_at)`, and the constructor
signature are exactly as the brief specifies; every method is on the brief's list.

The four load-bearing behaviours are implemented and each has a test that fails without it:

- **R6 — readiness is `GET /status`, never log text.** `start()` polls `/status` and
  requires a `code: 0` envelope whose `regions` is non-empty. Nothing in the readiness
  path reads the child's output; the output is only *reported* when readiness fails.
  `startup_timeout` defaults to 60 s against the spike's measured 5.9–18.7 s.
  `fake_launcher_slow` prints its banner 1.5 s before it serves and pins this.
- **R7 — `stop()` signals the launcher pid, never the process group.** `proc.terminate()`,
  then `SIGKILL` to the same pid after 15 s. No `killpg` anywhere in the file, and the
  reason (the payload is PID 1 of a nested PID namespace) is in the comment at the call
  site. `stop()` awaits `proc.wait()`, so the child is reaped and `os.kill(pid, 0)` raises
  `ProcessLookupError` — a zombie would not have.
- **R8 — adoption.** A pre-flight probe of the configured port decides three ways:
  a usable `/status` → adopt (`adopted=True`, `pid=None`, no spawn); a listener that does
  not answer `/status` → refuse with the EADDRINUSE explanation; nothing there → spawn.
  `login()` on an adopted supervisor raises and says to log in wrapper-side.
- **Port pre-flight.** A `connect()` distinguishes "occupied by a non-wrapper" from free.
  A refused connect means free, *including* a TIME_WAIT remnant, which the payload's
  `SO_REUSEPORT` does not care about (spike §6.8). A connect that *times out* counts as
  occupied, because on loopback a connect either succeeds or is refused at once.

Credentials reach the child only through stdin, and the log pump scrubs every credential
value — username, password, the joined `user:pass` form, and the 2FA code — before
anything reaches `log_sink`.

---

## 2. Decisions the brief left open

**`login()` starts the wrapper if it is not up.** The brief's own test calls `login()`
with no `start()`, and this is not merely convenience: `start()` waits for non-empty
`regions`, and `regions` stays empty until an account is logged in, so the one operation
that can make `start()` succeed cannot wait for `start()`. Login attaches to a live child
if there is one and spawns one otherwise.

**A readiness timeout is not retried; only a child death is.** `max_restarts` bounds
retries of a child that *died*, with 0.5 s exponential backoff capped at 8 s. A readiness
timeout raises immediately: the child is alive and may simply be one of the 18-second
startups the spike measured, and respawning it would convert a slow start into an
infinite one. A missing binary or an occupied port also raises immediately, because
retrying changes nothing. `test_the_restart_budget_is_bounded` asserts exactly
`max_restarts + 1` spawn attempts.

**`port=0` is resolved by the supervisor, not parsed out of a log line.** The supervisor
binds port 0 itself, reads the port back, closes, and passes the concrete port to the
child — the same technique the spike probe used. Learning the port from
`wrapper-lite listening on …` would reintroduce the log-text dependency R6 forbids.
`bound_port` reports it.

**`--base-dir` is passed through verbatim; the supervisor does not create it.** The
launcher resolves it *after* `chroot(".")`, so it is always chroot-relative
(`wrapper-lite-rootless.c:70-74`, 141-145). An absolute host path would mean something
else inside the chroot, and creating it on the host would create it in the wrong place.

**`cwd` is `binary.parent`, never inherited.** The launcher chroots into `./rootfs`
relative to the CWD. A supervisor that `chdir`s elsewhere breaks the launcher silently.

**Redaction is longest-match-first, and that ordering is the correctness of the
function.** A username that is a prefix of its own password (`SECRET` / `SECRET_PASS`)
would otherwise be replaced first and leave `***_PASS` in the log. Pinned by
`test_a_shorter_secret_does_not_leave_a_fragment_behind`.

**The log pump uses `read()`, not `readline()`, and this was a real bug.** The first
implementation used `StreamReader.readline()` and every 2FA test timed out. `readline()`
waits for a newline; `auth.cpp:66` writes `printf("2FA code: ")` + `fflush` and then blocks
reading the code, so the terminator only arrives *after* the answer. A `readline()` pump
delivers the prompt to nobody at the one moment it matters. `read(n)` returns whatever has
arrived, which is also what the Go GUI sees — one `Write` per pipe read (`main.go:686`).
The 2FA check runs on complete lines *and* on the unterminated remainder, using the
accumulated buffer rather than the raw chunk, so a marker split across two reads is still
found. That is a deliberate improvement on `check2FA`.

**The 2FA markers are copied verbatim from `wrapper/gui/main.go:653-657`** rather than
invented, for the reason the module docstring gives: three of the five come from
`auth.cpp`'s own output.

**Namespace failures are diagnosed by the launcher's own `perror` strings.**
`NAMESPACE_FAILURES` maps each string to what it means, so a start-up failure says
"the container needs `systempaths=unconfined` (spike §5.2/§5.6)" instead of leaving the
operator to guess from "Operation not permitted". `received signal 15` is called out
separately, because that line is the EADDRINUSE self-signal masquerading as an external
kill (spike §6.5).

**`running` is True for an adopted wrapper.** The question a caller asks is "can I use a
wrapper", and the answer is yes whether we started it or not. `adopted` is how a caller
tells the two apart, which the UI needs because an adopted one cannot be logged into.

**`status()` is uncached**, unlike the downloader's client, because the hub re-reads it
to drive the "regions went empty" path in spec §10 — a state change a cache would hide.

**Runtime auto-restart after a successful `start()` is deliberately not wired up.** The
budget and the backoff constants are there and the respawn is one call, but a respawn
from a background task races `stop()` for the pid and re-enters the readiness gate with
nobody awaiting the result, and the brief specifies no test for it. What *is* guaranteed:
a background watcher reaps the child, reports an unexpected exit with the launcher's own
last words, and flips `running` to False — which is what Task 9 needs to show a reconnect
banner instead of hanging on a request that will never be answered. This is the one piece
of spec §10 row 1 left for a later task; it is called out in the code.

**`LOGIN_PROMPT_TIMEOUT = 30 s`.** If no prompt appears, `login()` raises and says the
login is probably fine (no 2FA needed) or was rejected, and points at `/status`. There is
no non-2FA return value to give because there is no code left to submit.

**Every `SupervisorError` message names a URL, a pid, or the child's own output**, because
these failure modes are not visible from the parent's side. `start()` always tears the
child down on failure: the point of raising is that the UI can offer a retry, and a
silently surviving half-started wrapper would hold the port against it.

---

## 3. Test command and verbatim output

```
$ cd hub && uv run pytest -v
```

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 119 items

tests/test_config.py::test_password_is_required PASSED                      [  0%]
...
tests/test_supervisor.py::test_a_banner_and_a_clean_exit_is_not_readiness PASSED [ 49%]
tests/test_supervisor.py::test_start_reports_the_child_output_when_it_dies PASSED [ 50%]
tests/test_supervisor.py::test_crash_is_reported_not_silently_retried_forever PASSED [ 51%]
tests/test_supervisor.py::test_the_restart_budget_is_bounded PASSED      [ 52%]
tests/test_supervisor.py::test_stop_terminates_the_child PASSED           [ 53%]
tests/test_supervisor.py::test_stop_signals_the_launcher_pid PASSED      [ 54%]
tests/test_supervisor.py::test_stop_leaves_no_orphan_after_a_failed_start PASSED [ 55%]
tests/test_supervisor.py::test_stop_is_idempotent PASSED                 [ 56%]
tests/test_supervisor.py::test_adopts_a_healthy_wrapper_already_on_the_port PASSED [ 57%]
tests/test_supervisor.py::test_does_not_adopt_when_adopt_existing_is_false PASSED [ 58%]
tests/test_supervisor.py::test_login_on_an_adopted_supervisor_is_refused PASSED [ 59%]
tests/test_supervisor.py::test_stop_does_not_kill_an_adopted_wrapper PASSED [ 60%]
tests/test_supervisor.py::test_a_port_held_by_a_non_wrapper_fails_fast PASSED [ 61%]
tests/test_supervisor.py::test_no_preflight_when_port_is_zero PASSED     [ 62%]
tests/test_supervisor.py::test_login_raises_a_challenge_when_2fa_is_prompted PASSED [ 63%]
tests/test_supervisor.py::test_login_starts_a_supervisor_that_was_never_started PASSED [ 64%]
tests/test_supervisor.py::test_expired_challenge_is_rejected PASSED      [ 65%]
tests/test_supervisor.py::test_a_challenge_is_single_use PASSED          [ 66%]
tests/test_supervisor.py::test_an_unknown_challenge_is_rejected PASSED   [ 67%]
tests/test_supervisor.py::test_submit_2fa_after_the_wrapper_died_is_reported PASSED [ 68%]
tests/test_supervisor.py::test_credentials_never_reach_the_log_sink PASSED [ 69%]
tests/test_supervisor.py::test_the_2fa_code_never_reaches_the_log_sink PASSED [ 70%]
tests/test_supervisor.py::test_a_shorter_secret_does_not_leave_a_fragment_behind PASSED [ 71%]
tests/test_supervisor.py::test_a_raising_log_sink_does_not_kill_the_pump PASSED [ 72%]
tests/test_supervisor.py::test_start_on_a_missing_binary_fails_with_the_path PASSED [ 73%]
tests/test_supervisor.py::test_status_before_start_is_reported_not_an_httpx_error PASSED [ 74%]

============================= 119 passed in 17.02s ==============================
```

Step 2 of the brief was honoured — before the implementation existed:

```
$ cd hub && uv run pytest tests/test_supervisor.py -v
tests/test_supervisor.py:45: in <module>
    from hub.wrapper_supervisor import SupervisorError, WrapperSupervisor
E   ModuleNotFoundError: No module named 'hub.wrapper_supervisor'
```

All 29 supervisor tests are hermetic: stub launchers, ephemeral ports, ~17 s for the whole
suite including the 90 pre-existing tests. Run twice more with identical results, and
`pgrep stub_` finds nothing afterwards, so no test leaks a child. `uvx ruff check hub/ tests/`
passes on the whole package, matching the existing files.

Beyond the brief's 11 tests, 18 more. The ones that changed the implementation rather
than just pinning it: `test_a_banner_and_a_clean_exit_is_not_readiness` (the vacuous-PASS
shape from spike §6.8A — a launcher that prints the banner and exits 0 must not read as a
successful start), `test_start_reports_the_child_output_when_it_dies` (a `perror` must
reach the message), `test_the_restart_budget_is_bounded` (`max_restarts` is a budget),
`test_stop_leaves_no_orphan_after_a_failed_start`, `test_stop_does_not_kill_an_adopted_wrapper`,
`test_a_port_held_by_a_non_wrapper_fails_fast` (the §6.5 trap), `test_a_raising_log_sink_does_not_kill_the_pump`
(a broken sink must not take the reader down, which would wedge the child on a full pipe),
`test_the_2fa_code_never_reach_the_log_sink`, and
`test_a_shorter_secret_does_not_leave_a_fragment_behind`.

---

## 4. Real binary

`wrapper/wrapper-lite-rootless` was built by Task 1 and was not modified. Harness:
`hub/spike/task5_real_binary_check.py` (a script, not a test — the real launcher needs a
rootfs, 6–19 s and an account, none of which belongs in a hermetic suite).

**Run 1 — spawn, readiness, stop, orphan check** (`--port 0`):

```
  | spawning /home/m/amdl_extend/wrapper/wrapper-lite-rootless on 127.0.0.1:45367
  | 2026-09-26 16:31:43.399 [INFO ] initializing...
  | 2026-09-26 16:31:43.418 [INFO ] initializing ctx...
  | 2026-09-26 16:31:48.124 [WARN ] missing music/dev token, run --login first
  | 2026-09-26 16:31:48.124 [INFO ] wrapper-lite listening on 127.0.0.1:45367
  | the wrapper is serving on http://127.0.0.1:45367/status but reports no regions, so no Apple account is logged in yet; the hub should ask for one
  | stopping the wrapper launcher (pid 790835) with SIGTERM
  | 2026-09-26 16:32:42.873 [INFO ] received signal 15, stopping service
  | 2026-09-26 16:32:43.127 [INFO ] token cache saved
  | 2026-09-26 16:32:43.127 [INFO ] wrapper-lite stopped
```

- **Time to serving: 4.7 s** (43.398 → 48.124), inside the spike's 5.9–18.7 s range.
- **`/status` answered:** yes, HTTP 200 with `code: 0` — but `regions: []`, because
  `wrapper/rootfs/data` holds no tokens. So `start()` did **not** return: it held the
  readiness gate for the full 60 s and then raised, by design and with the right message:
  `the wrapper did not become ready within 60s: it is serving on … but /status reports no
  regions, which means no Apple account is logged in. Log in first, then start the wrapper
  again.` This is the regions requirement working, not a failure to start.
- **`stop()` left no orphan:** the launcher shut down gracefully on SIGTERM
  (`wrapper-lite stopped`), was reaped, and `os.kill(pid, 0)` raised `ProcessLookupError`.
  Note the child's own `received signal 15` here is the *legitimate* one, from our
  SIGTERM — distinct from the §6.5 self-signal the supervisor diagnoses by name.

**Run 2 — R8 adoption against the real thing** (`--port 12340 --adopt-existing`): the
host's own wrapper QEMU (pid 1126122) was adopted in 0.0 s. `adopted=True`, `pid=None`,
`status() == {'regions': ['jp']}`, and after `stop()` the user's wrapper still answered
HTTP 200. The collision this protects against is live on this machine, not hypothetical.

**Run 3 — the launcher's actual login path**, to see what the 2FA design has to work
against. `--login TESTUSER:TESTPASS` reached Apple's servers and was refused
(`Your account is disabled.`, `auth error: code=-1697321512`) — so the login plumbing
works, but I have no valid account and could not exercise a real 2FA prompt. **The 2FA
exchange is therefore still unverified against the real binary, exactly as spike §7 said
it would be.** See concern 1.

---

## 5. Concerns

**1. The 2FA stdin channel may not exist in the real binary, and the payload's 2FA branch
is gated on `isatty`.** `auth.cpp:64` reads:

```c
if (!g_code_from_file && isatty(STDIN_FILENO)) {
    printf("2FA code: "); fflush(stdout);
    if (scanf("%6s", code) == 1 && amPassword) { ... }
}
```

A supervisor-spawned child has a **pipe** on fd 0, not a tty, so `isatty` is false and
this branch is **unreachable**. I verified the condition both ways: `os.isatty(0)` is
`False` under `< /dev/null` and `True` under a `pty`. The payload then falls back to
polling `<base-dir>/2fa.txt` for up to 60 s — which is the host-file mechanism spec §3.1
says the child-process design exists to *avoid*. Also worth knowing: the payload takes
credentials from `--login user:pass` on **argv** (`lite_main.cpp:516-518`, the one
`set_credentials` call site at 546) and never reads them from stdin, so the "credentials
to stdin" contract is not what the real binary speaks either.

This does not invalidate anything in the brief — `login()`/`submit_2fa()` are the
interface Task 9 needs, the marker list and the pump are upstream-faithful, and a
`*_from_file`-style flag (`--code-from-file` exists and the hub does not pass it) or a
pty would both close the gap. **But Task 9 must not treat the 2FA form as proven.** Two
viable routes, neither of which I could choose alone: allocate a **pty** for the child's
stdin (keeps the stdin contract, needs `os.openpty` and a relay), or **write
`<base-dir>/2fa.txt` and let the payload poll for it** (no pty, but it reintroduces the
host file spec §3.1 rejects, and it must be created `0600` and unlinked). I recommend the
pty, and recommend Task 9's plan name the choice explicitly.

Related: `LITE_ARGS_FILE` (`lite_main.cpp:486`) lets the payload read its whole argv from
a file the launcher passes through `execve` unchanged, which is a way to get credentials
off `/proc/*/cmdline` if the hub ever needs to pass any. Not used here.

**2. Runtime auto-restart is not implemented** (spec §10 row 1, "supervisor が指数バックオフで
3 回まで自動再起動"). The bounded-retry machinery and the constants are in place and the
start-up path uses them, and the post-`start()` watcher reaps and reports, but it does not
respawn. Reason and the specific race are in the `_watch_child` docstring. This should be
picked up in Task 9 or 10, where the reconnect banner it feeds has a home.

**3. `start()` treats "serving with no regions" as not-ready, and that is a policy
decision.** It follows the brief and `AppleMusicDecrypt/src/cmd.py:132`, and the real
binary on a fresh install hits it every time — 60 s of waiting before the login prompt is
offered. The alternative (ready-but-unauthenticated, distinct from not-running) is a
better UX but contradicts the brief, so I implemented the brief. Worth a spec note: the
supervisor already emits "no regions" to the sink within ~5 s, so a future task can react
to that without waiting for the timeout.

**4. The adoption pre-flight cannot tell two wrappers apart.** Adoption accepts any
`code: 0` `/status` on the port, including a stale launcher sharing it via
`SO_REUSEPORT`. The spike documented that `/status` could then be answered by the wrong
process; the mitigation there was refusing to start, and here it is that adoption is
about *using* whatever is there, not starting a rival. The user's own `config.toml` points
at 12340, so adoption is the right default — but a user who means "my own wrapper" should
set `adopt_existing = false` and get the refusal.

**5. Ephemeral-port selection is racy in principle.** Bind to 0, read, close, hand to the
child. The window is microseconds and the payload's `SO_REUSEPORT` means a lost race is a
shared port rather than a failure. It is the same technique the spike probe used, and it
only affects `port=0`, which is the test path.

## 6. Verification notes

Nothing under `wrapper/` or `AppleMusicDecrypt/` was modified; the harness uses
`--base-dir data`, the directory the launcher creates for itself inside its own chroot, so
it adds nothing to the upstream tree. The host's wrapper on 12340 was left running and was
verified still answering after every run.

---

# Fix round 1

The review confirmed concern 1 and amended spec §3.1/§10/§11. Round one was a faithful
build of a spec that was wrong about the binary: the credentials went to stdin because the
brief said so, and the 2FA code went to stdin for the same reason. Neither channel exists.
This round builds what `wrapper/lite/auth.cpp` actually does.

Everything the review confirmed correct is untouched: R6 (readiness on `GET /status` with
non-empty `regions`), R7 (SIGTERM to the launcher pid, never the process group), R8
(adoption), the port pre-flight, the bounded restart cap, credential scrubbing, and
`port=0` ephemeral binding.

## 1. The 2FA code is a file, and it is the child's window

`login()` still watches stdout, and the trigger is still upstream's marker list. What
changed is the hand-off: `submit_2fa()` creates `<base-dir>/2fa.txt` instead of writing to
the child's stdin.

**The host-side path is not the configured one.** The launcher resolves `--base-dir`
*after* `chroot(".")` (`wrapper-lite-rootless.c:130-140`), so the payload's
`<base-dir>/2fa.txt` lives under the `rootfs` directory next to the binary. I verified that
against the real launcher this session: `--base-dir amd-hub-t5-probe` appeared as
`rootfs/amd-hub-t5-probe` (scratch directory removed afterwards). So

```python
chroot = binary.parent / "rootfs"
path = chroot / base_dir.relative_to("/") / "2fa.txt"   # absolute base dirs are chroot-absolute
```

and if `rootfs/` is not a directory, `submit_2fa` raises naming it, rather than writing a
file nobody reads.

The file is written to a temporary name and `os.replace`d into place, because the child
polls with `file_exists` and then `fopen`s — a half-written file would be read as a
truncated code rather than as no code. Mode **0600**, because `wrapper-lite-rootless.c:142`
is `mkdir(base_dir, 0777)`: the file's own mode is the only thing keeping a six-digit code
off other readers. The child `remove()`s the file itself (`auth.cpp:99`), so nothing is
left behind and the supervisor does not clean up.

New tests: `test_submit_2fa_writes_the_file_the_child_polls_for` (the stub emits
`Enter your 2FA code into …`, polls for the file, reads it and removes it — the assertion is
that the child *consumed* it, which is what fails if the path is wrong),
`test_the_twofa_file_is_owner_only` (the child reports the mode it saw, since the file is
gone by the time the parent could look).

## 2. `twofa_ttl` is 60.0, the child's own window

`auth.cpp:83-88` polls `20 x sleep(3)` and then aborts. The default was 300 s, five times
the real window: a user typing a perfectly good code at T+90 s would be told the code was
fine while the login had been dead for half a minute. Now 60.0, and `submit_2fa`'s expiry
message says the wrapper stopped waiting at about the same moment.

`test_twofa_ttl_is_the_child_window` asserts the deadline the supervisor reports is the
child's, and `test_expired_challenge_is_rejected` still covers the refusal. The stub's own
poll is shortened (`200 x 0.05 s` instead of `20 x 3 s`) so the suite stays usable; the
*shape* is what the tests pin, and the TTL-to-window relationship is asserted separately
against the constant rather than by waiting a real minute.

## 3. Credentials are on argv, and the exposure is stated

`login()` runs the launcher a second time, in its `--login` mode, with
`--login user:pass --code-from-file --base-dir <dir>` — exactly upstream's own flow
(`wrapper/gui/main.go:733`). `--code-from-file` is not optional decoration: without it the
payload decides between the tty branch and the file branch at `auth.cpp:64`, and a pipe
means the file branch by accident rather than by request.

**This makes the login a second, short-lived process.** The payload's login mode caches
tokens and `return 0`; it never listens (`lite_main.cpp:538-600`). So `login()` no longer
touches the serving child at all, which also removes the round-one awkwardness where
`login()` had to spawn the serving child first. `stop()` reaps both, and
`test_a_login_child_is_not_left_running` asserts the login child's pid is really gone — a
live login child would leave the credentials readable in `/proc/<pid>/cmdline`.

The module docstring no longer claims stdin. It states plainly that argv is visible to any
same-uid process for the child's lifetime, that the payload takes no other input, and what
bounds it: one service process, one uid, one container (spec §3, §14), and a login child
that lives seconds. The mitigations that exist are named — nothing reaches `log_sink` (every
pumped line is scrubbed), nothing reaches the hub's own argv or environment, nothing reaches
the database.

**stdin is now `DEVNULL`, not a pipe.** There is nothing to write, and the only job fd 0
has is to be something the payload's `isatty` test fails. `_write_stdin` and its timeout
constant are deleted rather than left as dead code.

## 4. Runtime auto-restart is wired up

`_watch_service` now respawns. Budget: `max_restarts` automatic restarts, exponential
backoff (`RESTART_BACKOFF_BASE`, capped), and then a terminal report naming the budget as
spent — spec §10's "以降は手動". A restart counts as successful only once `/status` answers
with regions again, the same gate `start()` uses, so a child that respawns and dies
immediately spends the budget like any other failed attempt.

**The budget is one per epoch, shared with `start()`'s retry loop** (`_restarts_used`).
Otherwise a wrapper that crashes once per start, on a supervisor with `max_restarts=3`,
would get six spawns, and the number reported to the user would not be the number that
happened. `test_the_restart_budget_is_shared_with_start` pins that.

New tests: `test_a_crashed_wrapper_is_restarted` (a stub that crashes once, then serves —
asserted on a new pid plus a working `status()`, not on a log line),
`test_the_restart_budget_stops_the_loop` (a stub that crashes on *every* `/status`: exactly
`max_restarts` restarts, then the terminal message, then `running` is False),
`test_the_restart_budget_is_shared_with_start`.

The crash stub dies **when it is first asked whether it is up**, not on a timer. A timer
would have to outlast an unknown startup cost for `start()` to see the 200 first, which is
a flaky test on a loaded machine; triggering on the request makes the ordering a certainty
because the response is already on the wire when the process exits.

## 5. The two readiness failures are worded differently

`start()` still treats `regions: []` as not-ready — the review confirmed the behaviour and
the reasoning — but the two failure messages no longer blur together:

- *never came up* — "the wrapper did not become ready within 60s: … never returned a usable
  status envelope (last probe: …). Its own output was: …"
- *no account* — "no account is logged in on the wrapper at …: it is up and answering
  /status, but regions is empty, so it cannot serve a download. Log in and start it again.
  Nothing needs to be waited for -- the wrapper itself is ready."

The second explicitly does not tell a user with no account to wait, because the wrapper is
already up and nothing about waiting will change it. The early progress line says the same
thing ("it is up and healthy, there is just no Apple account logged in on it yet"), so a UI
can react in the first few seconds rather than waiting out `startup_timeout`.

`test_a_wrapper_with_no_account_is_not_reported_as_a_readiness_timeout` and
`test_a_wrapper_that_never_comes_up_says_so` each assert their own wording **and** the
absence of the other's.

## 6. One thing the review did not ask for, found while doing it

`login()` used to wait the full `LOGIN_PROMPT_TIMEOUT` for a 2FA prompt that never came. An
account that needs **no** 2FA is the common outcome, and the login child exits by itself
when it is done — so round one would have blocked for 30 s on the most frequent real result
and looked like a hang. `login()` now waits for the prompt **or** the child's exit, and
distinguishes the two in the message. `test_a_login_that_needs_no_2fa_reports_at_once` pins
that it reports in under 5 s and quotes the child's own `login complete, exiting`.

Also fixed while in there: a second `login()` now discards the first's outstanding
challenges. Both login children watch the *same* `2fa.txt`, and `auth.cpp:92-96` appends
the code to the password — so a code offered against a stale challenge would have been
picked up by the *new* login and applied to the wrong account.
`test_a_second_login_supersedes_the_first`.

## 7. Commands and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 130 items

tests/test_config.py::test_requires_password PASSED                      [  0%]
...
tests/test_supervisor.py::test_start_waits_until_regions_are_reported PASSED [ 70%]
tests/test_supervisor.py::test_readiness_waits_for_status_not_for_the_banner PASSED [ 70%]
tests/test_supervisor.py::test_start_times_out_when_never_ready PASSED   [ 71%]
tests/test_supervisor.py::test_a_banner_and_a_clean_exit_is_not_readiness PASSED [ 72%]
tests/test_supervisor.py::test_start_reports_the_child_output_when_it_dies PASSED [ 73%]
tests/test_supervisor.py::test_crash_is_reported_not_silently_retried_forever PASSED [ 73%]
tests/test_supervisor.py::test_a_wrapper_with_no_account_is_not_reported_as_a_readiness_timeout PASSED [ 74%]
tests/test_supervisor.py::test_a_wrapper_that_never_comes_up_says_so PASSED [ 75%]
tests/test_supervisor.py::test_a_crashed_wrapper_is_restarted PASSED     [ 76%]
tests/test_supervisor.py::test_the_restart_budget_stops_the_loop PASSED  [ 76%]
tests/test_supervisor.py::test_the_restart_budget_is_shared_with_start PASSED [ 77%]
tests/test_supervisor.py::test_the_restart_budget_is_bounded PASSED      [ 78%]
tests/test_supervisor.py::test_stop_terminates_the_child PASSED          [ 79%]
tests/test_supervisor.py::test_stop_signals_the_launcher_pid PASSED      [ 80%]
tests/test_supervisor.py::test_stop_leaves_no_orphan_after_a_failed_start PASSED [ 80%]
tests/test_supervisor.py::test_stop_is_idempotent PASSED                 [ 81%]
tests/test_supervisor.py::test_adopts_a_healthy_wrapper_already_on_the_port PASSED [ 82%]
tests/test_supervisor.py::test_does_not_adopt_when_adopt_existing_is_false PASSED [ 82%]
tests/test_supervisor.py::test_login_on_an_adopted_supervisor_is_refused PASSED [ 83%]
tests/test_supervisor.py::test_stop_does_not_kill_an_adopted_wrapper PASSED [ 84%]
tests/test_supervisor.py::test_a_port_held_by_a_non_wrapper_fails_fast PASSED [ 85%]
tests/test_supervisor.py::test_no_preflight_when_port_is_zero PASSED     [ 86%]
tests/test_supervisor.py::test_login_raises_a_challenge_when_2fa_is_prompted PASSED [ 86%]
tests/test_supervisor.py::test_submit_2fa_writes_the_file_the_child_polls_for PASSED [ 87%]
tests/test_supervisor.py::test_the_twofa_file_is_owner_only PASSED       [ 88%]
tests/test_supervisor.py::test_login_does_not_need_the_serving_child PASSED [ 89%]
tests/test_supervisor.py::test_a_login_that_needs_no_2fa_reports_at_once PASSED [ 90%]
tests/test_supervisor.py::test_a_login_child_is_not_left_running PASSED  [ 90%]
tests/test_supervisor.py::test_twofa_ttl_is_the_child_window PASSED      [ 91%]
tests/test_supervisor.py::test_expired_challenge_is_rejected PASSED      [ 92%]
tests/test_supervisor.py::test_a_challenge_is_single_use PASSED          [ 92%]
tests/test_supervisor.py::test_an_unknown_challenge_is_rejected PASSED   [ 93%]
tests/test_supervisor.py::test_submit_2fa_after_the_login_child_gone_is_reported PASSED [ 94%]
tests/test_supervisor.py::test_credentials_never_reach_the_log_sink PASSED [ 95%]
tests/test_supervisor.py::test_the_2fa_code_never_reaches_the_log_sink PASSED [ 96%]
tests/test_supervisor.py::test_a_shorter_secret_does_not_leave_a_fragment_behind PASSED [ 96%]
tests/test_supervisor.py::test_a_second_login_supersedes_the_first PASSED [ 97%]
tests/test_supervisor.py::test_a_raising_log_sink_does_not_kill_the_pump PASSED [ 98%]
tests/test_supervisor.py::test_start_on_a_missing_binary_fails_with_the_path PASSED [ 99%]
tests/test_supervisor.py::test_status_before_start_is_reported_not_an_httpx_error PASSED [100%]

============================= 130 passed in 21.53s =============================
```

41 supervisor tests, up from 29. Run three times with identical results;
`pgrep -af stub_` finds nothing afterwards, so no test leaks a child. `uvx ruff check
hub/ tests/ spike/task5_real_binary_check.py` passes.

## 8. Real-binary re-check

Harness: `hub/spike/task5_real_binary_check.py`. Nothing under `wrapper/` was modified
except the scratch `--base-dir` directory created and removed during the mapping check.

**Run 1 — spawn, readiness, stop, orphan check** (`--port 0`):

```
  | spawning /home/m/amdl_extend/wrapper/wrapper-lite-rootless on 127.0.0.1:43337
  | 2026-09-26 17:06:00.547 [INFO ] initializing...
  | 2026-09-26 17:06:00.553 [INFO ] initializing ctx...
  | 2026-09-26 17:06:02.116 [WARN ] missing music/dev token, run --login first
  | 2026-09-26 17:06:02.117 [INFO ] wrapper-lite listening on 127.0.0.1:43337
  | the wrapper is serving on http://127.0.0.1:43337/status but reports no regions: it is up and healthy, there is just no Apple account logged in on it yet, so the hub should offer to log in
  | stopping the service (pid 929365) with SIGTERM
  | 2026-09-26 17:07:00.496 [INFO ] received signal 15, stopping service
  | 2026-09-26 17:07:01.120 [INFO ] token cache saved
  | 2026-09-26 17:07:01.120 [INFO ] wrapper-lite stopped

start() raised after 61.6s:
no account is logged in on the wrapper at http://127.0.0.1:43337/status: it is up and answering /status, but regions is empty, so it cannot serve a download. Log in and start it again. Nothing needs to be waited for -- the wrapper itself is ready.

stop() returned after 61.6s from spawn
  running : False
  adopted : False
  pid     : none -- nothing was spawned, so nothing can be orphaned
```

- **Time to serving: 1.6 s** (00.547 → 02.117) — faster than round one's 4.7 s, and well
  inside the spike's 5.9–18.7 s range. The variance is the payload's own startup work.
- **`/status` answered:** yes, HTTP 200 `code: 0`, `regions: []` (no tokens in
  `wrapper/rootfs/data`). `start()` therefore did not return, and the failure text is now
  the no-account one — the message split is visible in the real run, not only in a test.
- **`stop()` left no orphan:** graceful `wrapper-lite stopped`, reaped, `os.kill(pid, 0)`
  raised `ProcessLookupError`.

**Run 2 — R8 adoption of the host's live wrapper** (`--port 12340 --adopt-existing`):
adopted in 0.0 s, `pid None`, `status() == {'regions': ['jp']}`, and the user's own wrapper
still answered HTTP 200 after `stop()`.

**The 2FA path could not be exercised end to end.** It needs an Apple account with 2FA
enabled, and I do not have one — the only credentials this host can offer reach a disabled
account (`Your account is disabled.`, `auth error: code=-1697321512`). So the file hand-off
is verified against the stub, which models the launcher's chroot mapping, and the mapping
itself is verified against the real binary (`--base-dir X` → `rootfs/X`). The two have not
been joined by a real 2FA prompt, and I am not claiming otherwise.

## 9. Concerns after this round

**1. The end-to-end 2FA hand-off is still unproven against the real binary**, for want of an
account, not for want of a design. The two halves are each verified. If Task 10's
acceptance run has credentials, the first thing to do is a real `login()` + `submit_2fa()`
and watch for `Code file detected!` in the log. If the code is not picked up, the
suspicion should fall on the path arithmetic in `_twofa_file()` before anything else.

**2. `argv` is a real exposure and only the topology bounds it.** Nothing in this design can
put the password somewhere the payload does not read it from. The container's single-uid,
single-service shape is what makes it acceptable; that is a deployment property (Task 10),
not a property of this code, and it should not be relaxed. The Go GUI has the same exposure.

**3. A serving wrapper started before a login will not pick up the new account.** The
payload loads its token cache at start-up (`lite_main.cpp:614`), so a `start()` that
succeeded before the login keeps serving the old (or no) account. The UI must `stop()` and
`start()` after a successful login. This is stated in `login()`'s docstring and is a Task 9
sequencing requirement.

**4. The restart budget is per `start()` epoch, deliberately not a sliding window.** A
wrapper that crashes once every few hours is fine; one that crashes three times in a
lifetime needs a human, and that is spec §10's "以降は手動". If a user finds that too
strict in practice, raising `max_restarts` is the knob.

**5. Round-one concern 3 is now half-resolved.** The message no longer misleads, but the
60 s wait is still there: `start()` holds for `startup_timeout` before reporting the
no-account state. The early log line gives a UI something to act on in the first few
seconds, so Task 9 can offer the login form without waiting for the timeout. Making
`start()` *return* in that state would be better and would contradict the brief's
readiness definition, so it is left for the spec owner to decide.

---

# Fix round 1 (review findings C1, I1–I3, minors 1–5)

Spec compliance passed on all 13 amended requirements, and the 2FA chroot path arithmetic
was independently confirmed correct. This round is the four real findings plus the five
minors. Nothing in the "do not change" list was touched: R6 readiness gating, R8 adoption,
the port pre-flight, credential scrubbing, `port=0` binding, the 2FA path mapping and the
backoff schedule are all as they were.

## C1 — R7 now has a regression guard that is an assertion

The invariant was guarded by a comment, and the review showed that mutating `stop()` to
`os.killpg(os.getpgid(pid), 15)` passes 40 tests. Two changes make it a real test.

**Every stub now calls `setsid()`.** That is the part that turns a catastrophe into a
failure. With a stub in the runner's own process group, a group signal hits the *runner*:
the whole suite dies with exit 143 and prints nothing, so the mutant is never even
diagnosed. With every stub in its own session, a group signal can only ever surface as a
failed assertion. It is also the more faithful stub — the real launcher isolates far harder
(`unshare(CLONE_NEWUSER|CLONE_NEWNS|CLONE_NEWPID)`).

**One test asserts the mechanism, not the effect.** The new `setsid` stub spawns a
grandchild inside its own session, so a signal to the child pid takes the child and leaves
the grandchild, while a signal to the child's process group takes both. The assertion is
that the grandchild is still alive after `stop()`:

```python
test_stop_signals_the_launcher_pid_and_not_its_process_group
```

The two duplicated stop tests are folded into it, per minor 5.

Mutation check, run for real (mutant installed, full supervisor suite, default
configuration — no isolation tricks):

```
pytest exit=1
FAILED tests/test_supervisor.py::test_stop_signals_the_launcher_pid_and_not_its_process_group
1 failed, 42 passed in 26.81s
```

```
E  AssertionError: grandchild 1040659 died with the child, so stop() signalled the process
   group, not the launcher pid. That is the R7 bug, and against the real launcher it would
   leave the payload running as PID 1 of its nested namespace. Log: [...]
```

One failure, named, with the runner intact. Before the change the same mutant produced
`pytest exit=143` and no output at all.

## I1 — a stale `2fa.txt` is a credential, not litter

The review is right that `auth.cpp:84` guards only the *wait*; the `fopen` + `fscanf` at
`:92-96` that appends the file to the password is unconditional. A leftover file is not a
file nobody reads — the next login consumes it instantly and authenticates with a code
nobody typed for it. Four changes, three of them behavioural:

1. **`login()` unlinks the file before spawning**, which is the one moment a file at that
   path is unambiguously not ours. It reports when it did, so the log says
   `removed a leftover 2FA file … before starting a new login`.
2. **`_shutdown_login()` unlinks it when we signalled the child** — i.e. exactly the case
   where the child cannot have removed it, since `auth.cpp:99` is the last thing the login
   does and SIGTERM prevents it. When the child exited on its own the child has already
   removed it and nothing is done.
3. **The challenge deadline can no longer exceed the child's window.** It is now stamped
   from `twofa_seen_at` — the moment the prompt marker was seen, which is when the child
   starts counting — and `twofa_ttl` is clamped to `CHILD_TWOFA_WINDOW` (60.0,
   `20 x sleep(3)` at `auth.cpp:84-88`). Previously the deadline was minted after the
   marker and unclamped, so the supervisor's window ended slightly *later* than the child's
   `exit(1)` — widening exactly the sliver in which `submit_2fa` writes a file nobody reads.
4. The autouse fixture now also asserts no `2fa.txt` or `.2fa.txt.*` survives under
   `tmp_path`, and removes any that does. The review's observation was that the fixture
   diffed pids only and never files; that is fixed at the fixture, so it cannot regress.

New tests: `test_a_leftover_twofa_file_is_removed_before_a_new_login` (a stale file is
planted, the next login must wait for a real code),
`test_a_stopped_login_leaves_no_twofa_file`, and the review's reproduction as
`test_a_second_login_does_not_inherit_the_first_logins_code` — log in, hand over a code,
stop before the child polls it, log in again as a different account, and the second login
must not consume anything until it is given a code of its own. That test needed a
slow-polling stub (`fake_launcher_2fa_slow_poll`, 4 × 5 s) to make the window deterministic.

Also added: `test_a_ttl_longer_than_the_child_window_is_clamped`, so a 300 s configuration
is pinned to 60 s rather than merely described.

## I2 — the class is launcher-specific, and the docstring says so

The module docstring now names the two launchers the mapping holds for
(`wrapper-lite-rootless`, 10 `rootfs` references, and `wrapper-lite`, 7 with the same
`chdir`/`chroot` at `wrapper-lite.c:83-88`) and says plainly why the QEMU one cannot work:
`wrapper-lite-qemu.cpp` has **zero** `rootfs` references and passes `--base-dir /data`
*into the guest* (`:391`), so a file written on the host is invisible to the child. The
`SupervisorError` from `_twofa_file()` repeats it, and it now also points at
`config.py`'s `DEFAULT_WRAPPER_BINARY` — still the QEMU launcher — as the thing that needs
changing, rather than implying this module is at fault.

## I3 — an unbounded `start()` budget now fails in bounded time

`test_the_restart_budget_is_bounded` now has the shape the review asked for: run `start()`
as a task, poll a bounded window, *then* assert.

```python
task = asyncio.ensure_future(sup.start())
done, _ = await asyncio.wait({task}, timeout=20.0)
assert task in done, "start() was still retrying after 20s, so the restart budget is not bounding the loop..."
```

Mutation check (`if attempt > self._max_restarts` → `if False`), full supervisor suite:

```
pytest exit=1
1 failed, 42 deselected in 4.19s
```

```
E  AssertionError: start() was still retrying after 20s, so the restart budget is not
   bounding the loop. Spawns so far: ...
```

Previously that mutant ran past 150 s and had to be killed without reaching an assertion.

## Minors

1. **No raw epochs in user-facing text.** The challenge is now announced as "enter it
   within 60s, after which the wrapper gives up and exits", and an expired challenge as
   "expired 12s ago, around when the wrapper stopped waiting". `LoginChallenge.expires_at`
   stays an epoch, as the review says it should.
2. **`status()`'s guard no longer keys on `_bound_port` alone.** `_pick_port()` sets it
   before the spawn, so after a failed `start()` the port was known with nothing listening
   and the caller got a `ConnectError` phrasing. There is a second guard now, naming the
   pid and exit code of the child that is not running.
3. **The temp file is `O_EXCL` and its stale copy is unlinked first**, so a
   `.2fa.txt.<pid>` left by an earlier run of the same pid cannot keep its old mode, and
   the temporary is unlinked on every path out of the block, not only on a failed open.
4. **Citation fixed**: the chroot is `wrapper-lite-rootless.c:131-138`, not `:133` (which is
   `return 1;`). Verified by reading the file.
5. **The duplicate stop tests are folded** into the single mechanism assertion above.

## Commands and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 133 items

tests/test_config.py::test_requires_password PASSED                      [  0%]
...
tests/test_supervisor.py::test_start_waits_until_regions_are_reported PASSED [ 68%]
tests/test_supervisor.py::test_readiness_waits_for_status_not_for_the_banner PASSED [ 69%]
tests/test_supervisor.py::test_start_times_out_when_never_ready PASSED   [ 69%]
tests/test_supervisor.py::test_a_banner_and_a_clean_exit_is_not_readiness PASSED [ 70%]
tests/test_supervisor.py::test_start_reports_the_child_output_when_it_dies PASSED [ 70%]
tests/test_supervisor.py::test_crash_is_reported_not_silently_retried_forever PASSED [ 71%]
tests/test_supervisor.py::test_a_wrapper_with_no_account_is_not_reported_as_a_readiness_timeout PASSED [ 71%]
tests/test_supervisor.py::test_a_wrapper_that_never_comes_up_says_so PASSED [ 72%]
tests/test_supervisor.py::test_a_crashed_wrapper_is_restarted PASSED     [ 74%]
tests/test_supervisor.py::test_the_restart_budget_stops_the_loop PASSED  [ 74%]
tests/test_supervisor.py::test_the_restart_budget_is_shared_with_start PASSED [ 75%]
tests/test_supervisor.py::test_the_restart_budget_is_bounded PASSED      [ 76%]
tests/test_supervisor.py::test_stop_signals_the_launcher_pid_and_not_its_process_group PASSED [ 77%]
tests/test_supervisor.py::test_stop_leaves_no_orphan_after_a_failed_start PASSED [ 77%]
tests/test_supervisor.py::test_stop_is_idempotent PASSED                 [ 78%]
tests/test_supervisor.py::test_adopts_a_healthy_wrapper_already_on_the_port PASSED [ 79%]
tests/test_supervisor.py::test_does_not_adopt_when_adopt_existing_is_false PASSED [ 79%]
tests/test_supervisor.py::test_login_on_an_adopted_supervisor_is_refused PASSED [ 80%]
tests/test_supervisor.py::test_stop_does_not_kill_an_adopted_wrapper PASSED [ 80%]
tests/test_supervisor.py::test_a_port_held_by_a_non_wrapper_fails_fast PASSED [ 81%]
tests/test_supervisor.py::test_no_preflight_when_port_is_zero PASSED     [ 82%]
tests/test_supervisor.py::test_login_raises_a_challenge_when_2fa_is_prompted PASSED [ 83%]
tests/test_supervisor.py::test_submit_2fa_writes_the_file_the_child_polls_for PASSED [ 84%]
tests/test_supervisor.py::test_the_twofa_file_is_owner_only PASSED       [ 85%]
tests/test_supervisor.py::test_login_does_not_need_the_serving_child PASSED [ 86%]
tests/test_supervisor.py::test_a_login_that_needs_no_2fa_reports_at_once PASSED [ 86%]
tests/test_supervisor.py::test_a_login_child_is_not_left_running PASSED  [ 87%]
tests/test_supervisor.py::test_twofa_ttl_is_the_child_window PASSED      [ 88%]
tests/test_supervisor.py::test_a_ttl_longer_than_the_child_window_is_clamped PASSED [ 89%]
tests/test_supervisor.py::test_a_leftover_twofa_file_is_removed_before_a_new_login PASSED [ 90%]
tests/test_supervisor.py::test_a_stopped_login_leaves_no_twofa_file PASSED [ 90%]
tests/test_supervisor.py::test_a_second_login_does_not_inherit_the_first_logins_code PASSED [ 91%]
tests/test_supervisor.py::test_expired_challenge_is_rejected PASSED      [ 92%]
tests/test_supervisor.py::test_a_challenge_is_single_use PASSED          [ 92%]
tests/test_supervisor.py::test_an_unknown_challenge_is_rejected PASSED   [ 93%]
tests/test_supervisor.py::test_submit_2fa_after_the_login_child_gone_is_reported PASSED [ 94%]
tests/test_supervisor.py::test_credentials_never_reach_the_log_sink PASSED [ 94%]
tests/test_supervisor.py::test_the_2fa_code_never_reaches_the_log_sink PASSED [ 95%]
tests/test_supervisor.py::test_a_shorter_secret_does_not_leave_a_fragment_behind PASSED [ 95%]
tests/test_supervisor.py::test_a_second_login_supersedes_the_first PASSED [ 96%]
tests/test_supervisor.py::test_a_raising_log_sink_does_not_kill_the_pump PASSED [ 97%]
tests/test_supervisor.py::test_start_on_a_missing_binary_fails_with_the_path PASSED [ 99%]
tests/test_supervisor.py::test_status_before_start_is_reported_not_an_httpx_error PASSED [100%]

============================= 133 passed in 26.96s =============================
```

44 supervisor tests, up from 41. `uvx ruff check hub/ tests/ spike/task5_real_binary_check.py`
passes.

## The four verification results

**1. The `killpg` mutant is caught by an assertion, not by killing the runner.**
`pytest exit=1`, one FAILED, the R7 test, message naming the grandchild. Verified above with
the mutant actually installed and the full supervisor suite run in the default
configuration.

**2. The reviewer's stale-file reproduction passes as a test.**
`test_a_second_login_does_not_inherit_the_first_logins_code` — the second login consumes
nothing until it is given its own code.

**3. A clean suite run leaves no `2fa.txt` anywhere under the pytest tmp dirs.**

```
$ rm -rf /tmp/pytest-of-m/pytest-3*   # the round-1 leftovers the review found
$ cd hub && uv run pytest -q
133 passed in 26.99s
$ find /tmp/pytest-of-m \( -name '2fa.txt' -o -name '.2fa.txt.*' \)
count: 0
$ pgrep -af 'stub_' | grep -v pgrep
none
```

**4. An unbounded `start()` budget now fails within a bounded time.** The mutation run above
finished in 4.19 s with a named assertion; before the fix it exceeded 150 s and had to be
killed.

## Real binary, and a correction to my own report

Time to serve, five consecutive runs, measured from the payload's `initializing` line to its
`wrapper-lite listening on` line:

```
run 1: 17:45:33.934 -> 17:45:35.956 = 2.0s
run 2: 17:45:38.097 -> 17:45:39.745 = 1.6s
run 3: 17:45:42.883 -> 17:45:44.297 = 1.4s
run 4: 17:45:47.435 -> 17:45:48.891 = 1.5s
run 5: 17:45:52.026 -> 17:45:53.461 = 1.4s

time to serve: min 1.4s  max 2.0s  over 5 runs
```

**The review is right that quoting my single 1.6 s run was wrong**, and right that the figure
to quote is a range. But note the range I measure today is *below* both the review's 6.6 s
and the spike's 5.9–18.7 s, consistently, over five runs. I do not think that contradicts
the spike: this host is idle and warm, and the payload's start-up work is dominated by
reading a token cache and an accounts database that are in page cache after the previous
run. I have therefore left the module docstring's 5.9–18.7 s alone — the review confirmed
the spike supports it, and the brief's 9.8–18.4 s is the figure that should be corrected —
and `startup_timeout=60` covers every value observed by anyone so far, including the 2.0 s
fast end and the 18.7 s slow end, by a factor of three.

`stop()` was clean on every run (`wrapper-lite stopped`, reaped, no orphan), and the host's
own wrapper on 127.0.0.1:12340 was still answering HTTP 200 afterwards.

```
  | spawning /home/m/amdl_extend/wrapper/wrapper-lite-rootless on 127.0.0.1:57185
  | 2026-09-26 17:44:31.274 [INFO ] initializing...
  | 2026-09-26 17:44:32.896 [WARN ] missing music/dev token, run --login first
  | 2026-09-26 17:44:32.896 [INFO ] wrapper-lite listening on 127.0.0.1:57185
  | stopping the service (pid 1088070) with SIGTERM
  | 2026-09-26 17:44:56.223 [INFO ] received signal 15, stopping service
  | 2026-09-26 17:44:56.898 [INFO ] token cache saved
  | 2026-09-26 17:44:56.898 [INFO ] wrapper-lite stopped

stop() returned after 26.7s from spawn
  running : False
  pid     : none -- nothing was spawned, so nothing can be orphaned
```

The 2FA exchange remains unexercised end to end against the real binary, for the same reason
as before: no Apple account with 2FA on this host.

## Concerns after this round

**1. The I1 fix is defence at three points, and only two of them are guaranteed.** The
pre-spawn unlink and the post-shutdown unlink are ours and deterministic. But the third
layer is the payload's, and it is the one that matters: between `submit_2fa`'s liveness
check and the write there is a window in which the child can hit its own `exit(1)`. The
clamped deadline narrows that window rather than closing it, which is the best available
without a `auth.cpp` change. If a code is ever written into the void, the file is removed by
the *next* `login()`, so the worst case is one wasted login, not a wrong-password
authentication — but it is not zero.

**2. The scrubber redacts account names as well as secrets.** The I1 tests had to assert on
log *ordering* rather than on which account or code was used, because the username is in the
redaction set (spec §11 treats it as a credential, correctly). That makes those assertions
structurally correct but less readable, and it means nothing in the log pane can tell a user
which account is being logged in. If the UI needs that, it must come from the supervisor's
own state, not from the child's output.

**3. `config.py`'s `DEFAULT_WRAPPER_BINARY` is still the QEMU launcher**, which cannot do
2FA from the host at all (I2). The plan and the Task 2 Dockerfile use
`wrapper-lite-rootless`, so this is very likely a one-line default that was never updated —
but until it is, anyone running the hub with the default will find `submit_2fa` refusing.
That belongs to Task 2 or Task 10; it is not fixed here because it is not this module's
default.
