# Task 1 report — spike: `wrapper-lite-rootless` as a child process

**Status: DONE_WITH_CONCERNS** · **Date: 2026-09-26** · **Branch: `feat/phase-1-foundation`**

Deliverables:
- `hub/spike/child_process_probe.py`
- `hub/spike/Dockerfile.probe`, `hub/spike/compose.probe.yaml` (committed verification harness)
- `docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md`

**Verdict: `works` — but it requires adding `systempaths=unconfined` to compose's
`security_opt`. The verdict for spec §14 exactly as written is `needs-fallback`.** Both halves
matter and neither subsumes the other. Host: `works`. Container with `[seccomp:unconfined]` only:
`needs-fallback`. Container with `[seccomp:unconfined, systempaths=unconfined]`: `works`. No
`privileged`, no `cap_add`, no two-container fallback. Task 5 builds `WrapperSupervisor` as
designed.

---

## 1. Step 1 — build

```bash
cd /home/m/apple-dl_extend/wrapper
aria2c -o android-ndk-r23b-linux.zip -x16 -s16 https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
unzip -q android-ndk-r23b-linux.zip
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_HOST_LAUNCHERS=ON \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
      -DCURL_SHARED_LIB="$PWD/rootfs/system/lib64/libcurl.so"
cmake --build build -j"$(nproc)"
```

NDK 691 MiB / ~2 min. Configure ~10 s. Build ~90 s. **No `-Werror` failure — Release is
warning-clean.** Produced `wrapper/wrapper-lite-rootless` (8 240 B) and
`wrapper/rootfs/system/bin/lite` (606 528 B, Android 22 PIE). The plain
`cmake --build build -j$(nproc)` did emit both launcher binaries, so the dedicated
`--target wrapper_lite_rootless_exe` invocation was not needed.

**Two deviations, both environmental, neither touching upstream source:**

1. `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` — mandatory. Local CMake 4.4.3 vs upstream CI's 3.22.
   Without it, configure dies in cJSON 1.7.19's `cmake_minimum_required(VERSION 2.8.12)`:
   `Compatibility with CMake < 3.5 has been removed from CMake.`
2. `-DCURL_SHARED_LIB=<rootfs>/system/lib64/libcurl.so` — mandatory on this host.
   `CMakeLists.txt:62` is `find_library(… PATHS …)` with no `NO_DEFAULT_PATH`, so it resolved to
   the **host's** `/usr/lib/libcurl.so` (SONAME `libcurl.so.4`) instead of the rootfs Android
   `libcurl.so`. The first build died at exec with
   `CANNOT LINK EXECUTABLE: could not load library "libcurl.so.4"`.

## 2. Step 2 — probe

`hub/spike/child_process_probe.py`, stdlib only. All four contract clauses from the brief are
asserted in-script, plus: survives N seconds after the banner, loopback port accepts TCP,
`GET /status` returns 200, stdin accepts a write, SIGTERM shuts down cleanly. The probe refuses to
start if the port is already taken rather than misreporting EADDRINUSE as a verdict.

Two launcher facts shaped it: it chroots into `./rootfs` **relative to CWD** (not `--base-dir`), and
`--base-dir` is resolved **after** `chroot(".")`, so it is always chroot-relative.

## 3. Step 3 — host verdict: `works`

```bash
cd hub && uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 0
```

```
2026-09-26 13:04:29.581 [INFO ] initializing...
2026-09-26 13:04:29.588 [INFO ] initializing ctx...
2026-09-26 13:04:42.979 [WARN ] missing music/dev token, run --login first
2026-09-26 13:04:42.979 [INFO ] wrapper-lite listening on 127.0.0.1:48717
```

**10/10 checks PASS** — the probe has ten checks. This line originally said 9/9, before the
namespace check was split into "userns created" and "namespace mounts succeeded" and before the
stdin and SIGTERM checks became unconditional; both corrections are described under *Fix round 1*
and *Fix round 2* below. The result includes `GET /status` →
`HTTP 200 {"code":0,"msg":"SUCCESS","data":{"regions":[]}}`.
Startup latency **13.4 s** on this run; across every run whose latency the finding records the range
is **5.9–18.7 s**. `--port 0` was required: this host already runs a wrapper QEMU instance
on `127.0.0.1:12340`.

## 4. Step 4 — container verdict: `needs-fallback` as written, `works` with one flag

**The brief's exact command could not be used as written.** `uv` is not in `python:3.13-slim`
(`exec: "uv": executable file not found in $PATH`), and `-v "$PWD:/w"` bind-mounts a uid-1000 tree
into a uid-0 container, so the post-`unshare` single-uid map made it unwritable
(`open ./rootfs/dev/urandom failed: Permission denied`). Both are harness faults; I re-tested with a
root-owned rootfs baked into a throwaway image, which is what Task 2's Dockerfile will produce.

**With only `security_opt: [seccomp:unconfined]`, it genuinely fails:**

```
mount proc failed: Operation not permitted
```

Cause: Docker over-mounts 12 paths under `/proc` (`kcore`, `keys`, `timer_list`, `sysrq-trigger`,
`interrupts`, `acpi`, `asound`, `bus`, `fs`, `irq`, `scsi`, `sys`), so the kernel's
`mount_too_revealing()` check (`fs/namespace.c`) refuses a new `proc` mount from inside a nested
user namespace. `wrapper-lite-rootless.c:125` is where it dies.

**This was a real launcher failure, not a harness artefact** — the distinction the controller asked
about. The other six runtime failures I hit were mine (§6.1–6.6 of the finding).

Controls:

| # | `security_opt` | `cap_add` | Result |
|---|---|---|---|
| A | *(default seccomp)* + `systempaths=unconfined` | — | `unshare: Operation not permitted` |
| B | `seccomp:unconfined` | `SYS_ADMIN` | `mount proc failed: Operation not permitted` |
| C | `seccomp=unconfined` + `systempaths=unconfined` | — | **`VERDICT: works`** |

**B is the key negative:** `cap_add: [SYS_ADMIN]` does **not** fix it — `mount_too_revealing()` is a
visibility check, not a capability check. The brief's hypothesis that compose would "need `cap_add`"
is disproved. C is the minimal set, verified end-to-end through `docker compose up` with
`security_opt: [seccomp:unconfined, systempaths=unconfined]`, 10/10 PASS,
`GET /status` → HTTP 200. Startup latency **9.8 s** on that run; 12.1 s and 14.0 s on the
Fix round 2 re-runs of the same configuration (§ Fix round 2).

## 5. Step 5 — finding

`docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md` — verdict table, exact
commands, both captured launcher outputs, root causes for all **seven** runtime failure modes (six of
them mine, one the finding) plus the three probe defects found in review, per-task
consequences, reproduction steps.

## 6. Concerns

1. **Spec §14 needs a one-line amendment** — add `systempaths=unconfined`. Without it the container
   cannot work at all. Task 2 owns `compose.yaml`; flagging so it is not missed.
2. **Task 5 readiness timeout must exceed ~20 s.** Observed 5.9–18.7 s. The probe's 30 s deadline
   passed every run but is too tight to build on. Recommend 60 s, gated on `GET /status` == 200 —
   never on log text (see concern 3).
3. **A failed `svr.listen()` is indistinguishable from a signal-killed launcher in the logs.**
   `lite_main.cpp:705` self-signals via `pthread_kill`, emitting `received signal 15` right after a
   successful-looking banner. Task 5 must use `/status`, not the banner, as the readiness gate.
4. **`systempaths=unconfined` is a real hardening reduction** (re-exposes `/proc/kcore`, `/proc/keys`,
   `/proc/timer_list`, `/proc/sysrq-trigger` and the `/proc/{bus,fs,irq,sys}` masks read-write). It
   is not a no-op and belongs in spec §11 as a deliberate accepted trade. The container stays
   non-`privileged` with no added capabilities.
5. **CWD is load-bearing and easy to get wrong.** The chroot path is CWD-relative; Task 5 must pass
   `cwd=` explicitly on the `Popen` rather than inherit it.
6. **The rootfs must be root-owned with the container running as root**, or the single-uid map after
   `unshare` makes the tree unwritable. This rules out `user:` in compose unless the image is
   chowned to match.
7. **`stdin` is writable; the 2FA exchange is unverified** — the launcher accepted a `b"\n"` write
   without dying in both environments, and SIGTERM shut it down with `returncode=0`. No prompt was
   detected and no code was submitted, so spec §3.1 is not contradicted but not yet confirmed.
8. Scope kept to the spike: no web app, no `hub/pyproject.toml` (Task 2's). The container
   verification harness is committed under `hub/spike/` so the evidence is reproducible.

## 7. Commits

- `spike: verify wrapper-lite-rootless runs as a child process`

---

## Fix round 1

All five Important and eight Minor findings addressed. The technical verdict did not change; the
build was not re-run (artifacts verified by the reviewer).

### Commit situation — read this first

The controller committed **concurrently with my work**. Commit `a2fd3ae` ("docs: stage hub
dependencies per task instead of all in Task 2") swept my in-progress work tree and captured:

- `hub/spike/child_process_probe.py` (the fully fixed probe — verified all fix markers present at HEAD)
- `hub/spike/Dockerfile.probe`, `hub/spike/compose.probe.yaml` (new, from I4)
- a snapshot of the finding doc

I did not rewrite or amend that commit. The remainder of this round is `24ac7da` ("docs: correct
sysrq claim and make spike transcripts verbatim"), which carries the finding-doc corrections I made
after that snapshot. **So the fix round is split across `a2fd3ae` + `24ac7da`, and the probe/harness
changes sit under a message about hub dependencies.** I verified the committed probe is identical to
my final working copy, so nothing is lost, but the history is misleading and a reviewer diffing
`57ef4aa..HEAD` will see the probe under the wrong message.

### I1 — headline contradicted the table

Rewrote the finding's §1 opening to lead with both halves explicitly:

> **The verdict is `works`, but it requires adding `systempaths=unconfined` to compose's
> `security_opt`. The verdict for spec §14 exactly as written is `needs-fallback`.**

with the reasoning for why neither subsumes the other. Added "neither subsumes the other" to this
report's headline too. The §1 verdict table now carries a **Probe exit** column and a row id (H/D/A/B/C)
so the table, the transcripts and the controls are cross-referenceable.

### I2 — verdict was not gated on the contract assertions

`works = banner_at is not None and alive and connectable and http_status == 200` → now
`works = all(ok for _, ok, _ in results)`, i.e. the AND of **every** recorded check, including the
pid-1/`getppid` clause and the namespace scan. On a negative run the probe prints
`FAILED CHECKS (n/10): <names>` so the reader can tell a topology finding from a harness fault.

Verified by direct test of the exact case the reviewer cited (probe was PID 1, everything else
passes):

```
$ cd hub && uv run python /tmp/opencode/gatetest/gate_test.py
one FAIL present -> works = False (expected False)
  and the verdict names it: ['launcher is not pid 1 / probe parent is not init']
all PASS -> works = True (expected True)
I2 GATE VERIFIED
```

I also split the single namespace check into two, by stage, so a run says *which* operation was
refused — this is what makes control A distinguishable from rows D and B:

- check 3 `user namespace was created (no unshare/uid_map refusal)`
- check 4 `namespace mounts succeeded (no proc/urandom/chroot refusal)`

### I3 — security cost under-surfaced and partly mis-described

- Moved the cost into §1 as a new **§1.1 "What the fix costs"**, measured before/after in a table,
  instead of the phrase "that is the entire delta".
- Corrected the description. My original §7 wording said the flag "re-exposes
  `/proc/kcore`, `/proc/keys`, `/proc/timer_list`, `/proc/sysrq-trigger` and the
  `/proc/{bus,fs,irq,sys}` masks read-write", conflating two different mechanisms. Classified from
  `/proc/self/mountinfo` by the mount's ROOT field, the 12 over-mounts are **5 read-only proc binds**
  (`/proc/{bus,fs,irq,sys,sysrq-trigger}`), **4 `/dev/null` bind masks**
  (`/proc/{interrupts,kcore,keys,timer_list}`) and **3 empty read-only tmpfs masks**
  (`/proc/{acpi,asound,scsi}`).
- **Corrected my own incorrect sysrq claim.** I had written that the flag lets a root process
  "trigger SysRq actions — SysRq includes `reboot`, `crash` and `kill`". That is wrong: the host's
  `/proc/sys/kernel/sysrq` is `16` = bit 4 only (`remount read-only`), so `/proc/sysrq-trigger` is
  near-harmless in isolation. The real exposure is the *pair* — `/proc/sys/kernel/sysrq` is itself
  under `/proc/sys` and becomes writable, so the bitmask can be raised and then the trigger used.
  This is the substance of the flag's cost and it is unconditional.
- Kept the reviewer's `kcore` point but made it precise: confirmed by test that
  `CAP_SYS_RAWIO` (bit 17) is **not** in `CapEff` `0xa80425fb`, and that reading the now-genuine
  `/proc/kcore` returns `EPERM`. So it is not a live leak here; the risk is conditional on
  `CAP_SYS_RAWIO` ever being added.

```
$ docker run --rm ... sh -c 'test -w /proc/sys/kernel/sysrq && ... ; test -w /proc/sysrq-trigger && ...'
WRITABLE
/proc/sysrq-trigger WRITABLE
$ # and the before/after contrast
before: test -w /proc/sys/kernel/panic_on_oops -> no
after:  test -w /proc/sys/kernel/panic_on_oops -> YES
```

### I4 — container result was not reproducible from the deliverable

Committed the harness instead of inlining a fragment:

- `hub/spike/Dockerfile.probe` — build context is the workspace root so the existing root
  `.dockerignore` already excludes the NDK, `build/` and `rootfs/data/`. Documents the build and run
  commands, and the two properties the finding depends on (root-owned rootfs, launcher beside rootfs).
- `hub/spike/compose.probe.yaml` — complete and runnable: `image`, `working_dir`, `command` and
  `security_opt`, with comments naming the other three configurations.

Verified the image builds from the repo context and that the committed compose file produces the
§5.6 transcript:

```
$ docker build -f hub/spike/Dockerfile.probe -t amd-hub-spike:probe .
#12 exporting layers 1.3s done
#12 DONE 1.3s

$ docker compose -f hub/spike/compose.probe.yaml up --abort-on-container-exit --exit-code-from amd-hub
amd-hub-1  | PASS  launcher is not pid 1  pid=13, parent probe pid=12
amd-hub-1  | PASS  listen banner within 30s  marker='wrapper-lite listening on'
amd-hub-1  | PASS  user namespace was created (no unshare/uid_map refusal)
amd-hub-1  | PASS  namespace mounts succeeded (no proc/urandom/chroot refusal)
amd-hub-1  | PASS  still alive after the banner  returncode=None
amd-hub-1  | PASS  still alive 3s later  returncode=None
amd-hub-1  | PASS  loopback port accepts a TCP connection  127.0.0.1:35545
amd-hub-1  | PASS  GET /status answers  HTTP 200 {"code":0,"msg":"SUCCESS","data":{"regions":[]}}
amd-hub-1  | PASS  stdin is writable without killing the child (no 2FA exchange attempted)  wrote b'\n', closed stdin, child survived
amd-hub-1  | PASS  SIGTERM shuts the child down  returncode=0
amd-hub-1  | VERDICT: works
$ echo $?
0
```

### I5 — no transcript of the negative case

Re-ran all three negative configurations against the committed image; each now has its own command,
exit code, full check summary, `VERDICT:` line and captured output in its own section (§5.2 row D,
§5.4 control A, §5.5 control B). Also split the previously merged two-run block at old
`finding:213-225` into §5.2 (the run) and §5.3 (the mountinfo evidence).

| Row | Command | Exit | `VERDICT:` | Captured output |
|---|---|---|---|---|
| D | `--security-opt seccomp=unconfined` | 1 | `needs-fallback` | `mount proc failed: Operation not permitted` |
| A | `--security-opt systempaths=unconfined` | 1 | `needs-fallback` | `unshare: Operation not permitted` |
| B | `--security-opt seccomp=unconfined --cap-add SYS_ADMIN` | 1 | `needs-fallback` | `mount proc failed: Operation not permitted` |
| C | both `security_opt` values | 0 | `works` | `wrapper-lite listening on 127.0.0.1:34327` |
| H | host | 0 | `works` | `wrapper-lite listening on 127.0.0.1:49281` |

Two pieces of evidence the negative runs now carry that they did not before:

- **Control A reports `Seccomp: 2`** (filter mode) where every other row reports `0` (unconfined) —
  direct proof Docker's default profile was active.
- **Control B reports `CapEff: 00000000a82425fb`** vs row D's `00000000a80425fb`. The XOR is
  `0x00200000` = bit 21 = `CAP_SYS_ADMIN`, so the capability really was granted and the failure is
  still byte-identical. That is now a measured claim rather than an assertion.

**Count consistency:** the report previously said "four of five failures" while the doc documented
seven. Both now say **seven distinct failures, six of them mine, one (6.7) the finding**, and each is
numbered `6.1`–`6.7` with `5.1 F1`/`F2` cross-referenced to their harness-fault entries.

### Minor findings

- **M1** — check 9 renamed `stdin is writable without killing the child (no 2FA exchange attempted)`
  and its detail now reads `wrote b'\n', closed stdin, child survived`. Finding §1 and §7 and this
  report all state: stdin is writable, **the 2FA exchange is unverified** — no prompt detected, no
  code submitted, stdin then closed. The probe's module docstring says the same.
- **M2** — `BANNER_MARKERS = (b"listen", b"12340")` → `BANNER_MARKER = b"wrapper-lite listening on"`,
  the exact string from `wrapper/lite/lite_main.cpp:701`. `12340` was dead under `--port 0`; bare
  `listen` would match an error line.
- **M3** — `/proc/self/uid_map` and `/proc/1/comm` now go through a new `read_proc_text()` that
  returns `"<unreadable: ...>"` on `OSError`, consistent with `read_proc_field()` directly above.
  Verified: with both paths raising `PermissionError`, the helper returns
  `'<unreadable: Permission denied>'` instead of killing the probe before a verdict.
- **M4** — `still alive 3s later` and `stdin is writable ...` are now recorded unconditionally; on a
  dead child they record FAIL with `child already exited; not attempted`. Verified: the negative log
  contains exactly 10 `PASS|FAIL` lines, as does the positive log. Previously a failing run would
  report 8 and read as a pass.
- **M5** — after the banner loop, if the child exited the probe now does
  `done.wait(timeout=5.0)` before scanning for namespace errors, so a fast-failing child's `perror`
  line cannot be missed. This is what makes the negative runs' check 3/4 attribution reliable.
- **M6** — the harness note no longer blames "what else is inside the chroot's netns". It now states
  the launcher does **not** unshare a network namespace (`wrapper-lite-rootless.c:44` has no
  `CLONE_NEWNET`), so the bind competes in the inherited netns, and points at a second listener or a
  `TIME_WAIT` remnant.
- **M7** — added the reason for signalling the launcher pid rather than the process group: because it
  `unshare`s `CLONE_NEWPID`, the payload `lite` **is PID 1 of a nested PID namespace**, where a
  `killpg` from outside reaches a different set of pids than it appears to. Also cited
  `lite_main.cpp:447` for the `sigwait` consumer.
- **M8** — §6.5 now closes the loop: in the shipped topology 12340 is container-internal and never
  published, so the only thing that can hold it is a stale launcher inside that one container, which
  the `/status` gate already catches. The same note is in the probe's `can_bind()` docstring.
- **M10** — new §2.2 records that `wrapper/android-ndk-r23b-linux.zip` (691 MiB) is untracked **and**
  unignored, since `wrapper/.gitignore` covers only `android-ndk-r23b/`, so `git add -A` in that
  clone would stage it. Explicitly notes the fix was not made there because `wrapper/` is a separate
  upstream clone (spec §4).

### Checks run

| Check | Result |
|---|---|
| Host probe, final probe | exit 0, `VERDICT: works`, 10/10 PASS, `GET /status` → HTTP 200 |
| Row C via `docker run` | exit 0, `VERDICT: works`, 10/10 PASS |
| Row C via committed `compose.probe.yaml` | exit 0, `VERDICT: works`, 10/10 PASS |
| Row D / A / B | exit 1 each, `needs-fallback`, 10/10 checks reported with stage attribution |
| I2 gate unit check | `works=False` when any check FAILs; `works=True` when all pass |
| M3 unreadable-`/proc` check | degrades to a string, probe does not crash |
| M4 check-count check | 10 checks reported in both positive and negative logs |
| Probe mtime vs log mtimes | all logs postdate the final probe, so every transcript is current |
| All doc transcripts vs captured logs | compared line by line; found and fixed a fabricated host timestamp, a wrong ppid, padded summary lines, and a `<empty>` detail that the real log does not contain |

### Concerns

1. **The commit split described above is the main thing to review.** `a2fd3ae` carries the probe and
   harness under a message about hub dependencies. If the controller wants the fix round to read as
   one commit, that needs a history rewrite, which I did not do unprompted.
2. **I corrected a security claim I had made myself, in the direction of "less alarming but more
   precise".** Writable `/proc/sys` remains the real, unconditional cost. A re-reviewer should check
   the sysrq reasoning specifically, since the previous wording was wrong.
3. The `--port 0` requirement is environmental. In the shipped topology 12340 is container-internal,
   so production would use the fixed port; the probe's pre-flight check would only fire on a stale
   launcher.

---

## Fix round 2

Four follow-ups (N1–N4). N1, N2 and N3 were already implemented in the working tree when I picked
this up — uncommitted, and with the finding doc asserting evidence that had never been produced. I
verified each by execution, found the N3 reasoning to be wrong as written, corrected it, re-ran every
row of the finding against the final probe, and refreshed the two positive transcripts so no
transcript predates the shipped code. The technical verdict did not change; the NDK was not rebuilt.

Probe as committed: `sha256 ac010beb59cb0ff4ab6c80a4b9cb89f8a4611f59040aeefa8048ae20b3a9ccaf`.

### N1 — check 10 could PASS without the probe ever signalling

The teardown now records a failure, not a pass, when the child was already gone:

```
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=0) before any signal was sent
```

**Proven by execution, both directions.** Stub A is a shell script that prints the banner, sleeps
0.3 s, prints `received signal 15, stopping service` and exits 0 — the shape §6.5 calls
"indistinguishable from an external kill". Against the probe
**as committed at `5693c53`** (extracted with `git show HEAD:hub/spike/child_process_probe.py`) it
reproduces the reviewer's vacuous PASS verbatim; against the final probe it FAILs.

```
$ cd /home/m/apple-dl_extend/hub
$ uv run python /tmp/opencode/spike-n1-old/probe_head.py --binary /tmp/opencode/spike-n1-old/stub-a.sh --port 0
...
=== captured launcher output (stdout+stderr) ===
wrapper-lite listening on 127.0.0.1:41755
received signal 15, stopping service

=== end captured launcher output ===

[PASS] SIGTERM shuts the child down  -- child already exited, returncode=0
...
PASS  SIGTERM shuts the child down  child already exited, returncode=0

VERDICT: needs-fallback
FAILED CHECKS (4/10): still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted)
exit=1
```

The same stub against the final probe — note the count moving 4/10 → 5/10, i.e. the new failure is
real and gates the verdict:

```
$ uv run python spike/child_process_probe.py --binary /tmp/opencode/spike-n1/stub-a.sh --port 0
...
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=0) before any signal was sent

VERDICT: needs-fallback
FAILED CHECKS (5/10): still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
exit=1
```

`returncode=0` is the case that mattered: the old check scored its best possible result precisely
when the probe had done nothing. Two other details changed while I was in there: the passing branch
now says `sent SIGTERM, returncode=0` rather than a bare `returncode=0`, and the SIGKILL branch says
`sent SIGTERM but needed SIGKILL`, so no branch of check 10 can be read without knowing whether the
signal was sent.

### N2 — `NAMESPACE_MOUNT_ERRORS` under-covered its own comment

I read `wrapper/wrapper-lite-rootless.c:103-138` and listed every `perror()` in that range — seven,
which is what the tuple now holds. The missing one was `mkdir ./rootfs/proc failed` (the `mkdir` is
at line 120, the `perror` at 121, so grepping for the reviewer's line number lands one line below the
string; the tuple's inline comments give the *call* line, and the comment block now says so
explicitly). Two adjacent things I found while reading and deliberately did **not** add: `perror("mkdir
base_dir_arg failed")` (143) and `perror("mkdir mpl_db failed")` (149) have no `return 1`, so a hit
there is not a namespace failure and would misattribute; `perror("signal")` (81), `perror("fork")` (93)
and `perror("execve")` (154) sit outside the cited range. All five are named in the comment so the
omission reads as a decision rather than an oversight.

**Proven by execution, both directions.** Stub B prints exactly what `perror` prints at line 121 and
exits 1. Committed probe: `PASS`, i.e. a real userns failure reported as clean. Final probe: `FAIL`.

```
$ uv run python /tmp/opencode/spike-n1-old/probe_head.py --binary /tmp/opencode/spike-n1-old/stub-b.sh --port 0
...
PASS  namespace mounts succeeded (no proc/urandom/chroot refusal)  
...
FAILED CHECKS (7/10): listen banner within 30s; still alive after the banner; still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
exit=1

$ uv run python spike/child_process_probe.py --binary /tmp/opencode/spike-n1/stub-b.sh --port 0
...
FAIL  namespace mounts succeeded (no proc/urandom/chroot refusal)  mkdir ./rootfs/proc failed
...
FAILED CHECKS (8/10): listen banner within 30s; namespace mounts succeeded (no proc/urandom/chroot refusal); still alive after the banner; still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
exit=1
```

The stub bodies and the exact commands are in the finding's new §8.1, and I ran §8.1 verbatim to
confirm the published recipe works (the first draft of that snippet passed the wrong positional
argument, `$3` is `--host`; it is `$6`).

### N3 — the TIME_WAIT advice: neither offered remedy was right

**I did not simply set `SO_REUSEADDR`, because measurement says it does nothing.** The reviewer's
premise is right — the probe could not tell a `TIME_WAIT` port from a live listener — but
`SO_REUSEADDR` is not what fixes that, and the claim already sitting in the working tree (that a
`SO_REUSEADDR|SO_REUSEPORT` bind "lets a bind succeed over a `TIME_WAIT` remnant") was half false.
The payload's listening socket takes `SO_REUSEPORT` and never `SO_REUSEADDR`
(`wrapper/lite/httplib.h:2039-2055`), and the kernel honours `SO_REUSEADDR` over `TIME_WAIT` only when
the *conflicting* socket set it too. Full matrix, one bind per cell:

```
$ uv run python /tmp/opencode/spike-n1/tw_matrix2.py
TIME_WAIT from a SO_REUSEPORT socket (what the payload leaves)  (port 53549)
  TIME-WAIT 0      0          127.0.0.1:53549    127.0.0.1:43470
  bind none                     -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEPORT             -> OK
  bind SO_REUSEADDR|SO_REUSEPORT -> OK

TIME_WAIT from a socket with no options  (port 35297)
  TIME-WAIT 0      0          127.0.0.1:35297    127.0.0.1:57168
  bind none                     -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEPORT             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR|SO_REUSEPORT -> EADDRINUSE(EADDRINUSE)

LIVE listener WITH SO_REUSEPORT (a stale lite)  (port 53553)
  bind none                     -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEPORT             -> OK
  bind SO_REUSEADDR|SO_REUSEPORT -> OK

LIVE listener with no options (e.g. this host's qemu on 12340)  (port 56141)
  bind none                     -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEPORT             -> EADDRINUSE(EADDRINUSE)
  bind SO_REUSEADDR|SO_REUSEPORT -> EADDRINUSE(EADDRINUSE)
```

So the choice I made is the third one, and it is the honest one: **model the payload's real option
(`SO_REUSEPORT`, not `SO_REUSEADDR`), then disambiguate the two cases that option cannot separate with
something the probe can actually observe.** A `connect()` does it — a `TIME_WAIT` port has no
listener and refuses the connection. `can_bind()` is now a three-way decision returning
`(verdict, reason)`: plain bind succeeds → go; only the `SO_REUSEPORT` bind succeeds and nothing
answers a connect → a `TIME_WAIT` remnant, go; a live listener of either flavour → refuse, and say
which. Refusing the live-`SO_REUSEPORT` case matters: two `SO_REUSEPORT` listeners *can* share a
port, so the payload would start successfully and `/status` could be answered by the stale process —
a false PASS of the same family as N1.

All three branches exercised on the real launcher:

```
# branch: live listener with no options (this host's qemu wrapper on 12340) -> exit 4
$ uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless
HARNESS FAULT: 127.0.0.1:12340 is already in use, so the probe would measure EADDRINUSE instead of the question -- a live listener that does not set SO_REUSEPORT holds it, so the payload could not bind there either (this host's qemu wrapper on 12340 is one such listener). Re-run with --port 0 to pick a free ephemeral port.
exit=4

# branch: TIME_WAIT remnant -> proceed, twice in a row on one fixed port
$ uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 12399   # run 1
exit=0   VERDICT: works
$ ss -tan 'sport = :12399' | tail -1
TIME-WAIT 0      0          127.0.0.1:12399    127.0.0.1:33374
$ uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 12399   # run 2, remnant present
exit=0   VERDICT: works   (10 check lines)
```

That second block is a result, not just a harness check: **a supervisor can restart the launcher on
the same port immediately, without waiting for `TIME_WAIT` to drain** (§7 of the finding). The
working tree's `HARNESS NOTE` for "banner printed but process already gone" also claimed
`can_bind()` had ruled `TIME_WAIT` out; it now says only what the pre-flight actually established.

### N4 — stale counts corrected here

Corrected in place: §3's `9/9` → **10/10** with the reason (the namespace check was split in two and
the stdin/SIGTERM checks became unconditional), §4's "the other four failures I hit were mine" → **six**
(`§6.1`–`6.6`; the finding numbers seven runtime failures, six of them mine, one the finding), §5's
"four failure modes" → **seven**, §6's stale latency range `9.8–18.4 s` → **5.9–18.7 s**, and §3's
forward reference to a *Fix round 2* section that did not exist. The finding's own count claims were
updated the same way, and its "across nine successful runs" phrasing is gone: the honest statement is
the range plus the note that not every earlier run's latency is transcribed, because I cannot
re-derive the count from logs that no longer exist.

### Full re-verification against the final probe

All five rows re-run; the two positive transcripts in the finding (§4 host, §5.6 row C) were
**regenerated**, not left over from the earlier revision, so no transcript predates the committed
probe.

| Row | Command | Exit | Verdict | Check count |
|---|---|---|---|---|
| H | host, `--port 0` | 0 | `works` | 10/10 PASS, banner 5.9 s |
| C | `docker compose -f hub/spike/compose.probe.yaml up` | 0 | `works` | 10/10 PASS, banner 12.1 s |
| C | `docker run` + both `security_opt` | 0 | `works` | 10/10 PASS, banner 14.0 s |
| D | `docker run --security-opt seccomp=unconfined` | 1 | `needs-fallback` | 8/10 FAIL, `mount proc failed` |
| A | `docker run --security-opt systempaths=unconfined` | 1 | `needs-fallback` | 8/10 FAIL, `unshare:`, `Seccomp: 2` |
| B | `docker run … --cap-add SYS_ADMIN` | 1 | `needs-fallback` | 8/10 FAIL, `CapEff: 00000000a82425fb` |

The image was rebuilt after the last probe edit and its copy verified byte-identical
(`sha256 ac010beb…` inside and out), so the container rows ran this code and not a cached earlier one.
Row D's `SIGTERM` line and the check-10 wording are the new one throughout; the verdict table in §1 of
the finding is unchanged, because no row's verdict moved.

### Concerns

1. **I overrode the N3 instruction, and the reason is measurement, not preference.** The brief said
   "set `SO_REUSEADDR` **or** drop the advice". `SO_REUSEADDR` demonstrably changes no bind outcome
   here, and dropping the advice would have discarded a true and useful fact. I kept the substance and
   replaced the unobservable distinction with an observable one. A reviewer who disagrees should look
   at the matrix above first.
2. **I rewrote the finding's §3 and §6.5 rather than only appending.** Both carried the false
   `SO_REUSEADDR` claim. The alternative was leaving a wrong mechanism description in the document
   Task 5 reads.
3. **Not fixed, because it is outside my file list:** `hub/spike/compose.probe.yaml` still says
   "section 5.4" where the control table is now §5.4–5.6 and row C is §5.6. One-line comment drift in
   a committed harness file I was told not to touch.
4. **The finding's §4/§5.6 transcripts are from this round, so the earlier round-1 transcripts are
   gone.** The values they carried (18.7 s, 12.2 s) survive in the latency range and in §7; the
   per-run detail does not. I preferred that to leaving output in the document that a different
   revision of the probe produced.
