# 2026-09-26 — Spike: can `wrapper-lite-rootless` run as a child process?

**Task 1 of** `docs/superpowers/plans/2026-09-26-amd-hub-phase1.md` (spec §16 step 1, §15 row 1).
**Date:** 2026-09-26 · **Branch:** `feat/phase-1-foundation` · **Probe:** `hub/spike/child_process_probe.py`

---

## 1. Verdict

**The verdict is `works`, but it requires adding `systempaths=unconfined` to compose's
`security_opt`. The verdict for spec §14 exactly as written is `needs-fallback`.**

Both statements are load-bearing and neither subsumes the other. A reader who takes only the first
will write a compose file that does not work; a reader who takes only the second will needlessly
abandon the single-container topology.

| # | Environment | Configuration under test | Verdict | Probe exit |
|---|---|---|---|---|
| H | Host (no container) | plain `subprocess.Popen` | **`works`** | 0 |
| D | Container | **`security_opt: [seccomp:unconfined]` — spec §14 as written** | **`needs-fallback`** | 1 |
| A | Container | `security_opt: [systempaths=unconfined]` (default seccomp) | `needs-fallback` | 1 |
| B | Container | `security_opt: [seccomp:unconfined]` + `cap_add: [SYS_ADMIN]` | `needs-fallback` | 1 |
| C | Container | `security_opt: [seccomp:unconfined, systempaths=unconfined]` — **the fix** | **`works`** | 0 |

Row D is the configuration the brief actually put under test, and it fails. Row A shows
`seccomp:unconfined` is genuinely required. Row B shows `cap_add` does **not** help. Row C is the
minimal working set, and it is the answer to the topology question: **single container, no
`privileged`, no added capabilities, no two-container fallback.**

**Task 5 must build `WrapperSupervisor` as designed** — a web app that spawns the launcher as a
plain child process. Stdin is writable and SIGTERM shuts it down cleanly, so the pieces spec §3.1
depends on are present. Note what is *not* verified: no 2FA prompt was detected and no code was
submitted, so the 2FA exchange itself remains unverified (§7).

### 1.1 What the fix costs

`systempaths=unconfined` is **not** a free win, and the cost belongs in the same breath as the
verdict. Measured on this host, before and after:

| Path | spec §14 as written | with `systempaths=unconfined` |
|---|---|---|
| `/proc/sys` | read-only proc bind | **read-write** |
| `/proc/sys/kernel/panic_on_oops` | `test -w` → **no** | `test -w` → **YES** |
| `/proc/sys/kernel/sysrq` | not writable | **writable** |
| `/proc/sysrq-trigger` | read-only proc bind | **writable** |
| `/proc/irq`, `/proc/bus`, `/proc/fs` | read-only proc binds | read-write |
| `/proc/kcore` | `/dev/null` char device (reads empty) | genuine proc interface, but reads return `EPERM` |
| `/proc/keys`, `/proc/timer_list`, `/proc/interrupts` | `/dev/null` masks | genuine proc entries |

The material exposure is **a root process inside the container can now write arbitrary `/proc/sys`
kernel tunables**. That is the primary cost, and it is unconditional.

SysRq deserves care, because the obvious reading of this table is wrong. `/proc/sysrq-trigger`
becomes writable, but on this host `/proc/sys/kernel/sysrq` is `16`, i.e. bit 4 only
(`remount read-only`) — the dangerous bits (console level, keyboard, debug) are **not** set, so
`sysrq-trigger` is close to harmless in isolation. The exposure comes from the *combination*:
`/proc/sys/kernel/sysrq` is itself under `/proc/sys`, so it becomes writable too, and a root process
can raise that bitmask and then use `sysrq-trigger` — which does include `reboot`, `crash` and
signal-all-tasks. Two writable files, one of them the control knob for the other.

`/proc/kcore` also deserves the precise statement, because the intuitive reading is wrong in both
directions: it is **not** a live information leak here. `CapEff` does not include `CAP_SYS_RAWIO`
(bit 17 clear in `0xa80425fb`), and a read of the now-genuine `/proc/kcore` returns
`Operation not permitted`. The risk is conditional — if `CAP_SYS_RAWIO` is ever added, the flag has
removed the only thing standing between that capability and kernel memory.

Task 2 should record this in spec §11 as a deliberate, bounded trade: the container stays
non-`privileged`, gains no capabilities, and publishes only 8080. The reduced surface is confined to
`/proc`.

---

## 2. Build

```bash
cd /home/m/amdl_extend/wrapper
aria2c -o android-ndk-r23b-linux.zip -x16 -s16 https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
unzip -q android-ndk-r23b-linux.zip
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_HOST_LAUNCHERS=ON \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
      -DCURL_SHARED_LIB="$PWD/rootfs/system/lib64/libcurl.so"
cmake --build build -j"$(nproc)"
```

NDK r23b: 691 MiB, ~2 min at 3.0 MiB/s. Configure ~10 s. Build ~90 s. **No `-Werror` failure** —
the Release build is warning-clean.

Artifacts:

```
-rwxr-xr-x  8240  wrapper/wrapper-lite-rootless      ELF x86-64, GNU/Linux  (host launcher)
-rwxr-xr-x  606528 wrapper/rootfs/system/bin/lite   ELF x86-64 PIE, Android 22, NDK r23b (payload)
```

`BUILD_HOST_LAUNCHERS=ON` is the CMakeLists default, and the plain
`cmake --build build -j$(nproc)` did emit both launcher binaries, so driving the
`wrapper_lite_rootless_exe` target separately was not needed.

### 2.1 Two deviations from the brief's command line, both environmental

Neither touches upstream source. Both are load-bearing facts for Task 2's Dockerfile.

**(a) `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` — required, or configure fails outright.**

```
CMake Error at build/_deps/cjson-src/CMakeLists.txt:2 (cmake_minimum_required):
  Compatibility with CMake < 3.5 has been removed from CMake.
```

Local CMake is **4.4.3**; upstream CI (`ubuntu-22.04`, and upstream's own `Dockerfile` on
`debian:13.2`) gets CMake 3.22, where cJSON v1.7.19's `cmake_minimum_required(VERSION 2.8.12)` is
merely deprecated. `FetchContent` pulls cJSON and Dobby, so this bites **any** modern-CMake build.
The flag is what CMake itself recommends in the error text. **Task 2: pin the stage-1 image's CMake,
or pass this flag.**

**(b) `-DCURL_SHARED_LIB=<rootfs>/system/lib64/libcurl.so` — required, or the payload cannot start.**

`CMakeLists.txt:62` is `find_library(CURL_SHARED_LIB curl PATHS ${SYSTEM_LIB_DIR})` with **no
`NO_DEFAULT_PATH`**, so CMake also searches host paths. This machine has `/usr/lib/libcurl.so`
(from `libcurl-impersonate`, SONAME `libcurl.so.4`), which won:

```
CURL_SHARED_LIB:FILEPATH=/usr/lib/libcurl.so        # host glibc libcurl — WRONG
rootfs/system/lib64/libcurl.so                      # Android libcurl, SONAME libcurl.so — CORRECT
```

The first build therefore recorded `NEEDED libcurl.so.4` and died at exec time:

```
CANNOT LINK EXECUTABLE: could not load library "libcurl.so.4" needed by
  "/home/m/amdl_extend/wrapper/wrapper-lite-rootless";
  caused by library "libcurl.so.4" not found
```

Two things to note. First, that is the *Android* linker's message: the chroot and `execve` had
already succeeded, so the failure is purely a library-resolution mismatch. Second, the path in the
message is `argv[0]`, not the real executable — Android's linker reports the path it was handed,
and the kernel passes the original `argv`. A supervisor that spawns the launcher by absolute path
will see this misleading path in its logs. **Task 2: the stage-1 build image must not have a host
`libcurl.so` visible to `find_library`** (upstream's `debian:13.2` + `build-essential` has none,
which is why their Dockerfile works), or this flag must be passed explicitly.

**Cosmetic:** the shipped `libcurl.so` is `SONAME libcurl.so` while the NEEDED entry was
`libcurl.so.4`; the NDK's own `libdl.so` is still NEEDED but is absent from `rootfs/`. The Android
linker resolved both anyway, because it is lenient about unresolved NEEDED entries for these
libraries when the symbols are already satisfied. Worth a smoke test in Task 2 but not a blocker.

### 2.2 Repo hazard: the 691 MiB NDK zip is untracked and unignored

`wrapper/.gitignore` covers `android-ndk-r23b/` but **not** `android-ndk-r23b-linux.zip`, so the
691 MiB archive left behind by the download is untracked-but-not-ignored inside the upstream clone.
Anyone running `git add -A` or `git stash -u` in `wrapper/` would stage it. Not fixed here —
`wrapper/` is a separate upstream clone (spec §4) and editing its `.gitignore` is out of scope for
this task. Flagging it for whoever next touches that clone.

---

## 3. The probe

`hub/spike/child_process_probe.py`. Stdlib only, no dependencies. Ten checks; **every one of them
gates the verdict**, which is computed as the AND of all recorded results so that `VERDICT: works`
can never sit under a printed `FAIL`. On a negative run the probe prints `FAILED CHECKS (n/10)` and
names them.

The ten checks:

| # | Check | Kind |
|---|---|---|
| 1 | `launcher is not pid 1` / probe parent is not init | contract |
| 2 | `listen banner within 30s` | contract |
| 3 | `user namespace was created (no unshare/uid_map refusal)` | contract |
| 4 | `namespace mounts succeeded (no proc/urandom/chroot refusal)` | contract |
| 5 | `still alive after the banner` | supplementary |
| 6 | `still alive 3s later` | supplementary |
| 7 | `loopback port accepts a TCP connection` | supplementary |
| 8 | `GET /status answers` | supplementary |
| 9 | `stdin is writable without killing the child (no 2FA exchange attempted)` | supplementary |
| 10 | `SIGTERM shuts the child down` | supplementary |

Check 10 only passes when the probe actually sent `SIGTERM` and the child then exited
(`sent SIGTERM, returncode=0`). If the child was already gone the check records
`NOT TESTED: the child had already exited (returncode=N) before any signal was sent` and counts as a
failure — otherwise a launcher that printed the banner and self-exited 0 would be credited with a
clean shutdown it never performed.

Checks 3 and 4 are split by *stage* on purpose: a run then says which namespace operation was
refused — the userns creation itself, or the privileged-inside-the-userns mount work. That
distinction is what separates control A from rows D and B below.

The banner marker is anchored on the exact upstream string
`wrapper-lite listening on` (`wrapper/lite/lite_main.cpp:701`), not a bare `listen` (which would also
match an error line) and not the literal port (meaningless under `--port 0`).

Two facts about the launcher shaped the harness, both from `wrapper/wrapper-lite-rootless.c`:

- **It chroots into `./rootfs`, relative to the CWD — not to `--base-dir`.** The probe therefore
  passes `cwd=<dir containing rootfs>`, exactly as upstream's `entrypoint.sh` does
  (`./wrapper-lite-rootless` from `/app`).
- **`--base-dir` is resolved *after* `chroot(".")`**, so it is always chroot-relative: `/data` means
  `<cwd>/rootfs/data`. The probe passes a unique relative name and deletes the directory afterwards.

```bash
cd hub && uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 0
```

`--port 0` picks a free ephemeral port. **This host already runs a wrapper QEMU instance on
`127.0.0.1:12340`** (pid 1126122, `qemu-system-x86`), so the default port is not usable here; see
§6.5. The probe refuses to start rather than misreport an occupied port.

That pre-flight models the payload's own socket rather than guessing. The payload's listening socket
takes exactly one option: `SO_REUSEPORT`. httplib's `default_socket_options()`
(`httplib.h:2039-2055`) takes the `#ifdef SO_REUSEPORT` branch on Linux and never sets
`SO_REUSEADDR`. Measured on this host, one bind per option per row:

| what holds the port | plain | `+SO_REUSEADDR` | `+SO_REUSEPORT` |
|---|---|---|---|
| nothing | OK | OK | OK |
| `TIME_WAIT` left by an earlier `lite` | EADDRINUSE | EADDRINUSE | **OK** |
| a live listener that sets `SO_REUSEPORT` | EADDRINUSE | EADDRINUSE | **OK** |
| a live listener with no options | EADDRINUSE | EADDRINUSE | EADDRINUSE |

`SO_REUSEADDR` changes nothing in any row — the kernel honours it over `TIME_WAIT` only when the
*conflicting* socket set it too, and the payload never does. `SO_REUSEPORT` is the only option that
moves the outcome, and it is the one the payload uses. The pre-flight therefore tries a plain bind,
then a `SO_REUSEPORT` bind, and if only the second succeeds it disambiguates with a `connect()`: a
`TIME_WAIT` port has no listener and refuses the connection, so the probe proceeds, whereas a live
`SO_REUSEPORT` listener is a stale launcher that would end up *sharing* the port — the probe refuses
to start, because `/status` could then be answered by the wrong process. Only a listener with no
options at all (this host's qemu wrapper on 12340) is refused on the bind evidence alone. All three
branches are exercised: exit 4 on the live-listener branch, and two consecutive probe runs on one
fixed port — the second with a `TIME_WAIT` remnant present — both reaching `VERDICT: works` (§6.8).

---

## 4. Host verdict — `works` (row H, exit 0)

```
python           : 3.13.7  (/home/m/.local/share/uv/python/cpython-3.13.7-linux-x86_64-gnu/bin/python3.13)
probe pid/ppid   : 163670 / 163665
binary           : /home/m/amdl_extend/wrapper/wrapper-lite-rootless
cwd for launcher : /home/m/amdl_extend/wrapper
--base-dir       : spike-probe-163670  ->  .../wrapper/rootfs/spike-probe-163670 (inside the chroot)
--host/--port    : 127.0.0.1:33137
Seccomp          : 0 (0 = unconfined)
CapEff           : 0000000000000000
NoNewPrivs       : 0
uid_map          : '0          0 4294967295'
pid 1 is         : 'systemd'

$ .../wrapper-lite-rootless --base-dir spike-probe-163670 --host 127.0.0.1 --port 33137
  (cwd=/home/m/amdl_extend/wrapper)
```

Captured launcher output:

```
2026-09-26 13:43:58.517 [INFO ] initializing...
2026-09-26 13:43:58.524 [INFO ] initializing ctx...
2026-09-26 13:44:04.437 [WARN ] missing music/dev token, run --login first
2026-09-26 13:44:04.437 [INFO ] wrapper-lite listening on 127.0.0.1:33137
```

Contract summary:

```
PASS  launcher is not pid 1  pid=163671, parent probe pid=163670
PASS  listen banner within 30s  marker='wrapper-lite listening on'
PASS  user namespace was created (no unshare/uid_map refusal)  
PASS  namespace mounts succeeded (no proc/urandom/chroot refusal)  
PASS  still alive after the banner  returncode=None
PASS  still alive 3s later  returncode=None
PASS  loopback port accepts a TCP connection  127.0.0.1:33137
PASS  GET /status answers  HTTP 200 {"code":0,"msg":"SUCCESS","data":{"regions":[]}}
PASS  stdin is writable without killing the child (no 2FA exchange attempted)  wrote b'\n', closed stdin, child survived
PASS  SIGTERM shuts the child down  sent SIGTERM, returncode=0

VERDICT: works
```

**Startup latency: 5.9 s** from `initializing` to the listen banner. Across every run whose latency
this document records the range is **5.9–18.7 s** (§7); the number of successful runs is larger than
the number of recorded latencies, because earlier rounds' logs are not all transcribed here.

---

## 5. Container verdicts

All container rows use `hub/spike/Dockerfile.probe` (build and run commands in §8). The image
`COPY`s a root-owned rootfs rather than bind-mounting the host tree — see §5.1 for why that
distinction is not cosmetic. Every row below is a complete, independently reproducible transcript.

### 5.1 The brief's command could not be used as written — two harness faults

**F1 — `uv` is not in `python:3.13-slim`.**

```
$ docker run --rm python:3.13-slim uv --version
docker: Error response from daemon: failed to create task for container: failed to create shim task:
OCI runtime create failed: runc create failed: unable to start container process: error during
container init: exec: "uv": executable file not found in $PATH
```

The probe is stdlib-only, so plain `python` would work — but `docker run … python` makes the probe
**PID 1**, which fails the brief's own `getppid() != 0` clause for a reason unrelated to the
launcher (§6.4). The committed `Dockerfile.probe` installs uv to keep the probe off PID 1.

**F2 — `-v "$PWD:/w"` bind-mounts a uid-1000 tree into a uid-0 container.**

```
$ docker run --rm -v "$PWD:/w" -w /w/hub --security-opt seccomp=unconfined \
    python:3.13-slim python spike/child_process_probe.py --binary /w/wrapper-lite-rootless

uid_map      : '0          0 4294967295'
$ docker run --rm -v "$PWD:/w" -w /w python:3.13-slim stat -c "%u:%g %n" /w/wrapper/rootfs
1000:1000 /w/wrapper/rootfs          # bind mount brings host uid 1000; container runs as uid 0
$ docker run --rm -v "$PWD:/w" -w /w python:3.13-slim sh -c 'touch /w/wrapper/rootfs/dev/x && echo yes'
yes                                        # root CAN write it before the userns exists
```

But the launcher `unshare(CLONE_NEWUSER)`s **first**, writing a single-uid map (`0 0 1`). Files
owned by uid 1000 are then unmapped, so even `CAP_DAC_OVERRIDE` cannot reach them:

```
open ./rootfs/dev/urandom failed: Permission denied
```

**This is a bind-mount artefact, not a topology problem.** The real image `COPY`s the rootfs, so it
is root-owned and the single-uid map always covers it. All rows below use the baked image.

### 5.2 Row D — spec §14 as written: `needs-fallback` (exit 1)

This is the configuration the brief put under test, and it is the load-bearing negative result.

```
$ docker run --rm --security-opt seccomp=unconfined -w /w/hub amd-hub-spike:probe \
    uv run python spike/child_process_probe.py --binary /w/wrapper-lite-rootless --port 0

probe pid/ppid   : 12 / 1
Seccomp          : 0 (0 = unconfined)
CapEff           : 00000000a80425fb
uid_map          : '0          0 4294967295'
pid 1 is         : 'uv'

=== captured launcher output (stdout+stderr) ===
mount proc failed: Operation not permitted
=== end captured launcher output ===

PASS  launcher is not pid 1  pid=13, parent probe pid=12
FAIL  listen banner within 30s  exit=1, output=43B
PASS  user namespace was created (no unshare/uid_map refusal)  
FAIL  namespace mounts succeeded (no proc/urandom/chroot refusal)  mount proc failed
FAIL  still alive after the banner  returncode=1
FAIL  still alive 3s later  child already exited; not attempted
FAIL  loopback port accepts a TCP connection  127.0.0.1:60525
FAIL  GET /status answers  
FAIL  stdin is writable without killing the child (no 2FA exchange attempted)  child already exited; not attempted
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=1) before any signal was sent

VERDICT: needs-fallback
FAILED CHECKS (8/10): listen banner within 30s; namespace mounts succeeded (no proc/urandom/chroot refusal); still alive after the banner; still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
```

Note that check 3 **passes**: the user namespace was created successfully. The launcher then failed
at the first mount that needs `CAP_SYS_ADMIN` *inside* that new namespace.

### 5.3 Why: Docker over-mounts 12 paths under `/proc`

Classified from `/proc/self/mountinfo` by the mount's ROOT field, so `ro` and masked entries are not
conflated. Verbatim:

```
$ docker run --rm --security-opt seccomp=unconfined amd-hub-spike:probe \
    grep " /proc" /proc/self/mountinfo
1269 1267 0:93 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
122  1269 0:93 /bus /proc/bus ro,nosuid,nodev,noexec,relatime - proc proc rw
123  1269 0:93 /fs /proc/fs ro,nosuid,nodev,noexec,relatime - proc proc rw
124  1269 0:93 /irq /proc/irq ro,nosuid,nodev,noexec,relatime - proc proc rw
125  1269 0:93 /sys /proc/sys ro,nosuid,nodev,noexec,relatime - proc proc rw
126  1269 0:93 /sysrq-trigger /proc/sysrq-trigger ro,nosuid,nodev,noexec,relatime - proc proc rw
127  1269 0:101 / /proc/acpi ro,relatime - tmpfs tmpfs ro,size=4k,nr_inodes=1,...
129  1269 0:101 / /proc/asound ro,relatime - tmpfs tmpfs ro,size=4k,nr_inodes=1,...
130  1269 0:94 /null /proc/interrupts rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,...
131  1269 0:94 /null /proc/kcore rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,...
132  1269 0:94 /null /proc/keys rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,...
133  1269 0:101 / /proc/scsi ro,relatime - tmpfs tmpfs ro,size=4k,nr_inodes=1,...
134  1269 0:94 /null /proc/timer_list rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,...
```

(Options elided with `...` for the tmpfs lines only; nothing else changed.) Classified:

| Kind | Count | Paths |
|---|---|---|
| the real proc, read-write | 1 | `/proc` |
| **read-only proc bind** over the rw proc | 5 | `/proc/{bus,fs,irq,sys,sysrq-trigger}` |
| **`/dev/null` bind mask** (readable, yields zeros) | 4 | `/proc/{interrupts,kcore,keys,timer_list}` |
| **empty read-only tmpfs mask** | 3 | `/proc/{acpi,asound,scsi}` |

Twelve over-mounts in total. The distinction that matters for §1.1 is the second row: those five
paths are *real proc entries re-mounted read-only*, so the flag does not reveal anything new about
them — it hands back the write access that was taken away.

The kernel's `mount_too_revealing()` check (`fs/namespace.c`) refuses a new `proc` mount from
inside a nested user namespace unless an existing `proc` in the same mount namespace is **fully
visible**. With 12 over-mounts it is not, so `mount("proc", "./rootfs/proc", "proc", 0, NULL)` at
`wrapper-lite-rootless.c:125` returns `EPERM`. This is the whole of the finding.

With `systempaths=unconfined` the over-mounts are gone and `/proc` is a single mount:

```
$ docker run --rm --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
    amd-hub-spike:probe sh -c 'grep -c " /proc" /proc/self/mountinfo'
1
```

### 5.4 Control A — default seccomp: `needs-fallback` (exit 1)

```
$ docker run --rm --security-opt systempaths=unconfined -w /w/hub amd-hub-spike:probe \
    uv run python spike/child_process_probe.py --binary /w/wrapper-lite-rootless --port 0

Seccomp          : 2 (0 = unconfined)
CapEff           : 00000000a80425fb

=== captured launcher output (stdout+stderr) ===
unshare: Operation not permitted
=== end captured launcher output ===

PASS  launcher is not pid 1  pid=13, parent probe pid=12
FAIL  listen banner within 30s  exit=1, output=33B
FAIL  user namespace was created (no unshare/uid_map refusal)  unshare:
PASS  namespace mounts succeeded (no proc/urandom/chroot refusal)  
FAIL  still alive after the banner  returncode=1
FAIL  still alive 3s later  child already exited; not attempted
FAIL  loopback port accepts a TCP connection  127.0.0.1:50823
FAIL  GET /status answers  
FAIL  stdin is writable without killing the child (no 2FA exchange attempted)  child already exited; not attempted
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=1) before any signal was sent

VERDICT: needs-fallback
FAILED CHECKS (8/10): listen banner within 30s; user namespace was created (no unshare/uid_map refusal); still alive after the banner; still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
```

`Seccomp: 2` (filter mode) versus `0` (unconfined) in every other row is the direct evidence that
Docker's default profile was active. It blocks `unshare` with `CLONE_NEWUSER` outright, before any
mount is attempted. This confirms upstream's `seccomp:unconfined` is genuinely required — and the
check-3/check-4 split is what makes this failure distinguishable at a glance from rows D and B,
where the userns succeeds and the mount is what fails.

### 5.5 Control B — `cap_add: [SYS_ADMIN]`: `needs-fallback` (exit 1)

The brief predicted the container might "need `cap_add`". It does not.

```
$ docker run --rm --security-opt seccomp=unconfined --cap-add SYS_ADMIN -w /w/hub \
    amd-hub-spike:probe uv run python spike/child_process_probe.py \
    --binary /w/wrapper-lite-rootless --port 0

Seccomp          : 0 (0 = unconfined)
CapEff           : 00000000a82425fb

=== captured launcher output (stdout+stderr) ===
mount proc failed: Operation not permitted
=== end captured launcher output ===

PASS  launcher is not pid 1  pid=13, parent probe pid=12
FAIL  listen banner within 30s  exit=1, output=43B
PASS  user namespace was created (no unshare/uid_map refusal)  
FAIL  namespace mounts succeeded (no proc/urandom/chroot refusal)  mount proc failed
FAIL  still alive after the banner  returncode=1
FAIL  still alive 3s later  child already exited; not attempted
FAIL  loopback port accepts a TCP connection  127.0.0.1:58823
FAIL  GET /status answers  
FAIL  stdin is writable without killing the child (no 2FA exchange attempted)  child already exited; not attempted
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=1) before any signal was sent

VERDICT: needs-fallback
FAILED CHECKS (8/10): listen banner within 30s; namespace mounts succeeded (no proc/urandom/chroot refusal); still alive after the banner; still alive 3s later; loopback port accepts a TCP connection; GET /status answers; stdin is writable without killing the child (no 2FA exchange attempted); SIGTERM shuts the child down
```

`CapEff` differs from row D by exactly `0x00200000` = bit 21 = `CAP_SYS_ADMIN`, so the capability
really was granted. The failure is byte-identical to row D. `mount_too_revealing()` is a
*visibility* check, not a capability check: `CAP_SYS_ADMIN` cannot make a partially-masked `/proc`
fully visible. **Adding `cap_add: [SYS_ADMIN]` would be pure attack surface for zero benefit.**

### 5.6 Row C — the fix: `works` (exit 0)

```yaml
# hub/spike/compose.probe.yaml, abbreviated to the part that matters
services:
  amd-hub:
    image: amd-hub-spike:probe
    working_dir: /w/hub
    command: ["uv","run","python","spike/child_process_probe.py",
              "--binary=/w/wrapper-lite-rootless","--port=0"]
    security_opt:
      - seccomp:unconfined
      - systempaths=unconfined
```

```
$ docker compose -f hub/spike/compose.probe.yaml up --abort-on-container-exit --exit-code-from amd-hub
amd-hub-1  | PASS  launcher is not pid 1  pid=13, parent probe pid=12
amd-hub-1  | PASS  listen banner within 30s  marker='wrapper-lite listening on'
amd-hub-1  | PASS  user namespace was created (no unshare/uid_map refusal)  
amd-hub-1  | PASS  namespace mounts succeeded (no proc/urandom/chroot refusal)  
amd-hub-1  | PASS  still alive after the banner  returncode=None
amd-hub-1  | PASS  still alive 3s later  returncode=None
amd-hub-1  | PASS  loopback port accepts a TCP connection  127.0.0.1:34075
amd-hub-1  | PASS  GET /status answers  HTTP 200 {"code":0,"msg":"SUCCESS","data":{"regions":[]}}
amd-hub-1  | PASS  stdin is writable without killing the child (no 2FA exchange attempted)  wrote b'\n', closed stdin, child survived
amd-hub-1  | PASS  SIGTERM shuts the child down  sent SIGTERM, returncode=0
amd-hub-1  | VERDICT: works
$ echo $?
0
```

Under `docker run` with the same two flags, exit 0, 10/10 PASS, and:

```
=== captured launcher output (stdout+stderr) ===
2026-09-26 13:44:24.021 [INFO ] initializing...
2026-09-26 13:44:24.035 [INFO ] initializing ctx...
2026-09-26 13:44:38.014 [WARN ] missing music/dev token, run --login first
2026-09-26 13:44:38.014 [INFO ] wrapper-lite listening on 127.0.0.1:53201
```

**Startup latency: 14.0 s** via `docker run`, 12.1 s via compose.

---

## 6. Failure modes, and which were mine

Seven distinct runtime failures were hit. **Six were mine; only 6.7 was the finding.** Three further
probe defects were found in review rather than at runtime, and are recorded in §6.8.

**6.1 Host `libcurl.so` hijacked `find_library`** — my environment. `CMakeLists.txt:62` resolved to
`/usr/lib/libcurl.so`, so the payload recorded `NEEDED libcurl.so.4` and the Android linker refused
to start it. Fixed by passing `-DCURL_SHARED_LIB`. §2.1(b).

**6.2 `BufferedReader.read(n)` deadlock in my own probe** — my bug. `read(4096)` blocks until the
buffer fills or EOF, so a long-lived child produced **zero** captured output while serving HTTP
perfectly. Fixed by reading with `os.read(fd, …)` in the reader thread. Worth remembering: a
"no output" result from a pipe-based probe is a probe bug until proven otherwise.

**6.3 `uv` missing from `python:3.13-slim`** — my harness. §5.1 F1.

**6.4 `getppid() == 0` in the container** — my harness. `docker run … python` makes the probe
**PID 1**, so the brief's own clause fails for a reason that has nothing to do with the launcher.
Installing uv in the image fixed it. Under row C the chain is launcher pid 13 → probe pid 12 →
`uv` pid 1: three deep, none of them init except `uv`, which is the most favourable position
available in a container. (The probe now reports this as a FAIL, and that FAIL gates the verdict.)

**6.5 `received signal 15, stopping service` immediately after the banner** — my harness, and a
genuinely nasty trap. **This host already runs a wrapper QEMU instance on `127.0.0.1:12340`**, so
the bind failed with `EADDRINUSE`, `svr.listen()` returned at once, and `lite_main.cpp:705`
(`pthread_kill(sig_thread.native_handle(), SIGTERM)`) made the payload signal *itself* — producing a
log line that reads exactly like an external kill. The probe now pre-checks that the port is free
and exits with a `HARNESS FAULT` instead of a verdict. The pre-flight binds the way the payload binds
(plain, then `SO_REUSEPORT`) and disambiguates with a `connect()` when only the second succeeds, so a
`TIME_WAIT` remnant is no longer mistaken for a live listener and no longer aborts the probe, while a
live listener — with or without `SO_REUSEPORT` — still does (§3, §6.8). Note the launcher does
**not** unshare a network namespace (`wrapper-lite-rootless.c:44` has no `CLONE_NEWNET`), so the
bind competes in the netns it inherited — on the host that means the whole loopback, including that
QEMU instance. **In the shipped topology this class of collision is closed**: 12340 is
container-internal and never published (spec §3, §14), so the only thing that can hold the port is a
stale launcher inside that one container, which the `/status` readiness gate already catches.

**6.6 uid-1000 bind mount made the rootfs unwritable** — my harness. §5.1 F2.

**6.7 `mount proc failed: Operation not permitted`** — **the finding.** §5.2.

### 6.8 Three probe defects found in review, not at runtime

None changed any verdict in §1 — every row already FAILed on other checks — but the first two were
the same defect class as a test that asserts nothing, so they are recorded here. The third is a
claim this document itself got wrong, which is worse.

**A vacuous PASS on check 10.** Round 1 recorded `rc == 0` as `PASS SIGTERM shuts the child down`
when the child had *already* exited, i.e. a check named after an action the probe never took. It
showed up on exactly the code path §6.5 calls "indistinguishable from an external kill". Reproduced
against a stub launcher that prints the banner and exits 0, the committed probe printed

```
PASS  SIGTERM shuts the child down  child already exited, returncode=0
```

and the probe now prints, for the same stub,

```
FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=0) before any signal was sent
```

**A missing error marker.** `NAMESPACE_MOUNT_ERRORS` cited
`wrapper-lite-rootless.c:103-138` but omitted `mkdir ./rootfs/proc failed` (the `mkdir` is at line
120, the `perror` at 121), so a launcher failing at that `perror` scored
`PASS namespace mounts succeeded` — a real userns failure reported as clean. Every `perror()` on that
line range is now listed, which a stub reproduces:

```
FAIL  namespace mounts succeeded (no proc/urandom/chroot refusal)  mkdir ./rootfs/proc failed
```

**A `TIME_WAIT` claim that was false, and the wrong remedy for it.** An intermediate revision of this
document asserted that the pre-flight's second bind carried `SO_REUSEADDR | SO_REUSEPORT` and that
this made a `TIME_WAIT` remnant non-blocking. Half of that is false, and measurably so:
`SO_REUSEADDR` changes no outcome at all, because the kernel honours it over `TIME_WAIT` only when
the conflicting socket also set it, and the payload never does (`httplib.h:2047-2049` takes the
`SO_REUSEPORT` branch). The `SO_REUSEPORT` half is true, and the conclusion survives: two
consecutive probe runs on one fixed port, the second started with a `TIME_WAIT` remnant on it, both
reach `VERDICT: works`. The full matrix is in §3. Worth carrying to Task 5: a supervisor can restart
the launcher on the same port immediately, without waiting for `TIME_WAIT` to drain.

The stubs are throwaway `sh` scripts under `/tmp` that satisfy the probe's only preconditions on
`--binary` (an executable file whose parent directory contains `rootfs/system/bin`); the commands and
their verbatim output are in the task-1 report. Nothing under `wrapper/` was modified.

---

## 7. Consequences for the tasks that follow

**Task 5 — `WrapperSupervisor`: build it as designed. No topology change.**

- Plain `subprocess.Popen(argv, stdout=PIPE, stderr=STDOUT, stdin=PIPE)`. `stderr` must be folded
  into `stdout`: all `LOG_*` output goes to stderr (`wrapper/lite/logger.h:59`), and it is
  unbuffered, so lines arrive promptly. Verified.
- **Readiness timeout must exceed ~20 s.** Observed banner latency 5.9–18.7 s across the runs
  recorded in §4 and §5.6. The probe's own 30 s deadline passed every run, but that is uncomfortably
  tight; use **60 s**.
- **`stdin` is writable; the 2FA exchange is unverified.** The launcher accepted a `b"\n"` write on
  stdin without dying, in both environments. That is all that was tested: no prompt was detected,
  no code was submitted, and stdin was then closed. Spec §3.1's design is *not* contradicted, but
  Task 5 must still verify prompt detection and code delivery end to end before relying on it.
- **Stop with `SIGTERM` to the launcher pid only, never `killpg`.** Two reasons.
  `wrapper-lite-rootless.c:24` installs a handler that forwards the signal to its chrooted child,
  and `lite` consumes SIGTERM via `sigwait` (`lite_main.cpp:447`), so signalling the launcher is
  sufficient and graceful. And because the launcher `unshare`s `CLONE_NEWPID`, the payload `lite`
  **is PID 1 of a nested PID namespace** — where a signal aimed at "the process group" is not what
  the supervisor means to express, and where `killpg` from outside reaches a different set of pids
  than it appears to. Verified: `returncode=0`, graceful `wrapper-lite stopped`.
- **A failed `svr.listen()` is indistinguishable from a signal-killed launcher in the logs**
  (§6.5). Gate readiness on `GET /status` returning HTTP 200, never on log text.
- Do **not** add `cap_add: [SYS_ADMIN]`. Proven useless (§5.5) and strictly increases attack
  surface, against spec §11.

**Task 2 — Dockerfile / compose:**

- Add `systempaths=unconfined` to `compose.yaml`'s `security_opt`. Without it the container cannot
  work at all (§5.2).
- Record the cost of that flag in spec §11: writable `/proc/sys` for a root process in the
  container, including `/proc/sys/kernel/sysrq`, which together with a writable
  `/proc/sysrq-trigger` gives reboot/crash (§1.1). It is a deliberate, bounded trade, not a free win.
- Stage 1 needs `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` (or a pinned CMake ≤ 3.x) and must not expose
  a host `libcurl.so` to `find_library` (§2.1).
- The image layout must be `<dir>/wrapper-lite-rootless` + `<dir>/rootfs`, and the process CWD must
  be `<dir>` — the chroot path is CWD-relative, so a supervisor that `chdir`s elsewhere breaks it.
  Set `cwd=` explicitly on the `Popen`; never rely on the inherited CWD.
- The rootfs must be **root-owned and the container must run as root**, or the post-`unshare` uid
  mapping (a single-uid map) will make the tree unwritable (§5.1 F2). This rules out `user:` in
  compose unless the image is chowned to match.
- `hub/spike/Dockerfile.probe` and `hub/spike/compose.probe.yaml` are a working reference for both
  of the above; do not copy them verbatim, since they carry the probe rather than the app.

---

## 8. Reproducing

The verification harness is committed, so nothing load-bearing lives in `/tmp` any more.

```bash
# 0. build prerequisites (once) -- see section 2
cd /home/m/amdl_extend/wrapper && cmake --build build -j"$(nproc)"

# 1. build the spike image from the workspace root, so the root .dockerignore applies
cd /home/m/amdl_extend
docker build -f hub/spike/Dockerfile.probe -t amd-hub-spike:probe .

# 2. host (row H)
cd hub && uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless --port 0

# 3. the fix (row C) -- via compose, exactly as shipped
docker compose -f hub/spike/compose.probe.yaml up --abort-on-container-exit --exit-code-from amd-hub
docker compose -f hub/spike/compose.probe.yaml down

# 4. the negatives.  Swap security_opt on the docker run to reproduce D, A, B:
#    D  --security-opt seccomp=unconfined
#    A  --security-opt systempaths=unconfined
#    B  --security-opt seccomp=unconfined --cap-add SYS_ADMIN
docker run --rm --security-opt seccomp=unconfined -w /w/hub amd-hub-spike:probe \
  uv run python spike/child_process_probe.py --binary /w/wrapper-lite-rootless --port 0
```

Each command exits 0 for `works`, 1 for `needs-fallback`, 3 for a malformed `--binary`, and 4 for
the occupied-port `HARNESS FAULT`.

### 8.1 Reproducing the two probe-defect fixes (section 6.8)

The probe's only preconditions on `--binary` are that it is an executable file and that its parent
directory contains `rootfs/system/bin`, so a shell script can stand in for the launcher. Two stubs,
each in its own directory:

```bash
mkdir -p /tmp/spike/{a,b}/rootfs/system/bin
cd /home/m/amdl_extend/hub

# stub A: reaches the banner, then exits 0 on its own -- no signal is ever sent
cat > /tmp/spike/a/stub.sh <<'EOF'
#!/bin/sh
# $1=--base-dir $2=<dir> $3=--host $4=<addr> $5=--port $6=<port>
echo "wrapper-lite listening on 127.0.0.1:$6"
sleep 0.3
exit 0
EOF

# stub B: dies at wrapper-lite-rootless.c:121, perror("mkdir ./rootfs/proc failed")
cat > /tmp/spike/b/stub.sh <<'EOF'
#!/bin/sh
echo "mkdir ./rootfs/proc failed: Operation not permitted"
exit 1
EOF
chmod +x /tmp/spike/{a,b}/stub.sh

# N1: check 10 must not PASS
uv run python spike/child_process_probe.py --binary /tmp/spike/a/stub.sh --port 0 | grep 'SIGTERM'
# N2: check 4 must FAIL
uv run python spike/child_process_probe.py --binary /tmp/spike/b/stub.sh --port 0 | grep 'namespace mounts'
```

Against the probe as committed, the first prints
`FAIL  SIGTERM shuts the child down  NOT TESTED: the child had already exited (returncode=0) before
any signal was sent` and the second prints
`FAIL  namespace mounts succeeded (no proc/urandom/chroot refusal)  mkdir ./rootfs/proc failed`.
Against the previous revision of the probe (`git show HEAD~1:hub/spike/child_process_probe.py`) the
same two stubs print `PASS  SIGTERM shuts the child down  child already exited, returncode=0` and
`PASS  namespace mounts succeeded`.

Environment: Linux, Docker 29.8.0, CMake 4.4.3, NDK r23b, uv 0.12.9,
CPython 3.13.7 (host) / 3.13.15 (container).
