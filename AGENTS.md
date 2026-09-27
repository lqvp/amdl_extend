# AGENTS.md — amdl_extend

Workspace notes for agents. General engineering rules (Japanese-language guidelines, git safety,
"don't delete/move files without asking") come from the global `~/.config/opencode/AGENTS.md` and
still apply. This file only records repo-specific facts that are hard to infer from the code.

## Layout: one repo, two pinned submodules

`/home/m/amdl_extend` is the git repo. `hub/` is the only code it owns; the two upstream
trees are **submodules**, pinned to a commit rather than floating on a branch:

| Path | Gitlink | Upstream remote | Branch | Stack |
|------|---------|-----------------|--------|-------|
| `AppleMusicDecrypt/` | `8b609df` | `WorldObservationLog/AppleMusicDecrypt` | `v3` | Python 3.11+ TUI client (the downloader) |
| `wrapper/` | `c61dea9` | `itouakirai/wrapper` | `lite` | C++ `wrapper-lite` HTTP backend |

They were whole-directory `.gitignore` entries until the image learned to build the wrapper
itself; nothing recorded which upstream revision was in use, and a fresh clone could not
produce the image at all. **Cloning needs `--recurse-submodules`**, and moving a pin is a
deliberate act rather than a routine one: `wrapper`'s `rootfs/` is 101 tracked `.so` files, so
a re-pin changes the payload the image ships.

**Their own `.gitignore` files still apply inside them, and the root's does not.** That is not
a detail: `git check-ignore` at the root refuses a path inside a submodule outright ("is in
submodule 'wrapper'"), so a question about `wrapper/.gitignore` has to be asked of the wrapper
repository. `test_the_image_builds_the_wrapper_instead_of_copying_prebuilt_artifacts` does.

## Always `cd AppleMusicDecrypt` before running anything

Three paths are resolved **relative to the current working directory**, and `main.py` never chdirs:

- `config.toml` — `Config.load_from_config()` default arg (`src/config.py:119`)
- `assets/prefetch_template.json` — `EMBEDDED_TEMPLATE_PATH` (`src/decrypt.py:35`)
- `downloads/` — default `dirPathFormat`

Launching from the workspace root does not error loudly: it silently skips the embedded FairPlay
prefetch template and starts making `/key` RPCs it doesn't need to. Same for `python qemu/deploy.py`
and `python qemu/login.py`, which insert `..` on `sys.path` and expect the package root as CWD.

## Commands

```bash
cd AppleMusicDecrypt
uv sync                          # setup (uv 0.12.9 at ~/.local/bin/uv)
uv run python main.py            # full-screen TUI (default)
uv run python main.py --legacy-ui   # v2-style plain REPL, for minimal terminals
```

There is **no Python test suite.** `pyproject.toml` declares `pytest` in the dev group, but no
`test_*.py` / `conftest.py` exists anywhere, and `.gitignore` lists `/tests/` — so tests cannot be
committed either. The only available automated check is a syntax/import pass:

```bash
.venv/bin/python -m compileall -q src main.py
```

Correctness is otherwise only observable by running the TUI against a live wrapper instance
(`status`, then `qa <url>`, then `dl <url>` on a single track).

Helpers, all run from `AppleMusicDecrypt/`:

```bash
python scripts/migrate_config.py            # old config.toml -> current schema; writes config.toml.bak
python qemu/deploy.py --type lite          # download local wrapper backend, then PATCHES config.toml
python qemu/login.py                       # one-shot login against the local backend
```

`qemu/deploy.py` rewrites `[localInstance]` in your gitignored `config.toml` as a side effect.
Read the diff before running it if you care about local settings.

## The biggest trap: the two repos are not wired together

The client needs a wrapper HTTP API at `[instance] url` (default `127.0.0.1:12340`). **Login happens
wrapper-side, not in this client** — `login`/`logout` commands only work against wrapper-*manager*.

What the pieces actually look like on disk right now:

- `[localInstance] wrapperType` defaults to `"manager"` → expects `wrapper-manager-qemu` from
  **`WorldObservationLog/wrapper-manager` v2**, a repo that is *not* cloned here.
- `AppleMusicDecrypt/qemu/` ships only `deploy.py` and `login.py`. No launcher binary, no guest
  assets. So `enable = true` cannot work until `qemu/deploy.py` has been run.
- The local `wrapper/` clone builds **`wrapper-lite-qemu`**, i.e. the `"lite"` backend.
- `wrapper/qemu/` contains only build scripts — no `bin/`, no `data.img` / `vmlinuz-lite-qemu` /
  `lite-initramfs.cpio.gz`.

To actually run the local clone against the client you must do all three: set `wrapperType = "lite"`,
build `wrapper-lite-qemu` (below), and point `launcherBin` at the resulting binary.

Also note `localInstance.hostPort` defaults to **8080**, not 12340 — enabling it overwrites
`[instance] url` to `127.0.0.1:8080` (`src/cmd.py:55`).

## Two more traps, found the hard way

**Stale `__pycache__` will silently lie to you.** CPython validates a `.pyc` by source
mtime+size, so a source file rewritten within the same size and second keeps its old bytecode.
This produced a *phantom* test failure twice during Phase 1 — the source provably contained a
clamp that `inspect.getsource` showed and the bytecode did not. Before believing a failure in
`hub/`, run `find hub -name __pycache__ -type d -exec rm -rf {} +` and re-run. A test that fails
only sometimes, or a mutation that "survives", is usually this.

**`hub/` is a separate project with its own strict review.** It reimplements nothing, but its
dedup logic is subtle enough that a wrong answer is either a silent re-download or a silent skip.
Two invariants are load-bearing and must not be broken casually:

- `normalize()` is applied to **both** sides of every comparison, and the album-name path uses
  `strip_track_prefix=False`. If the index build and the lookup disagree, every album lookup
  misses and nothing is ever skipped.
- `dedup.find_duplicate` is a **pure** function over a `LibraryScan`. It must not grow a cache, a
  filesystem read, or an index table — the library on disk is the single source of truth, on
  purpose, because folders get moved and renamed outside the app.

**`RipperHost` holds the process working directory.** `src/config.py` loads `config.toml`
through a *relative* path, so the seam `chdir`s into `AppleMusicDecrypt/` for its whole
lifetime and restores it on `close()`. Every path `hub/` uses is therefore absolute — **a
relative path added later will resolve against `AppleMusicDecrypt/` and fail silently on
reads.** If you add a path setting, make it absolute at the boundary. Relatedly,
`config_path` is effectively pinned to `<vendor>/config.toml`: `ConfigCreator` calls
`load_from_config()` with its own default and there is no upstream hook, so a
hub-owned config bind-mounted elsewhere would be read by a *different* file than the
caller named.

Run its tests from `hub/`: `cd hub && uv run pytest -v`.

## Config changes touch three places

`src/config.py` (pydantic models + `CONFIG_VERSION`), `config.example.toml` (documented defaults),
and your gitignored `config.toml`. When adding a key:

1. Add the field to the matching model in `src/config.py` with a default.
2. Add it to the same `[section]` in `config.example.toml` **with its explanatory comment** — that
   file is the user-facing documentation, and `scripts/migrate_config.py` reads its defaults.
3. Bump `CONFIG_VERSION` in *both* `src/config.py` and `config.example.toml`. They must match; a
   mismatch makes every launch print a bogus "configuration file is out of date" warning.

Local `config.toml` currently differs from the example in exactly one place:
`region.language = "ja"` (example ships `zh-Hant-HK`). Preserve that when regenerating.

`.github/workflows/win-build.yml` builds the Windows nightly by string-replacing the **literal lines**
`launcherBin = ""` and `enable = false` in `config.example.toml`. Reformatting or renaming those lines
silently breaks the Windows artifact.

## Client architecture

**DI is `creart`.** Every singleton is resolved at use site via `it(Class)`. Creators are registered
in `main.py` in strict dependency order (Logger → Config → API → Wrapper → Decryptor → Measurer →
TaskTree). Adding a new singleton means adding an `AbstractCreator` subclass *and* an `add_creator`
call there; registration order is load-bearing because `create()` runs eagerly and touches config.

**TUI is prompt_toolkit**, not Textual. `src/tui/log_sink.py` bridges loguru output into the log pane.
In exit paths, never call `os._exit` — it skips alternate-screen/raw-mode/mouse-tracking teardown and
leaves the user's terminal broken. Use `app.exit()` (TUI) or the private `_LegacyExit` exception
(legacy REPL); see `InteractiveShell.handle_exit` in `src/cmd.py:430`.

**Two decryption paths, don't conflate them:**

- FairPlay (everything except `aac-legacy` and music videos): `temari`, a bundled Rust cdylib.
  Handles are cached per `(adam_id, uri)`. The content-independent prefetch template is fetched
  exactly once with `adam_id="0"`; if `assets/prefetch_template.json` loads, no `/key` call happens
  at all. `download.streamDecrypt` (default `true`) decrypts while downloading via
  `StreamDecryptor`; `false` falls back to whole-file download + `decrypt_par` per fragment.
- Widevine (`aac-legacy`, music videos): `src/legacy/` + pywidevine for the license, pure-Python
  AES-CBC for samples. `src/legacy/` is *not* dead code — it is the only path for those two cases.

**Container handling is hand-written.** `src/mp4.py` (1763 lines) is a pure-Python ISO-BMFF
parser/re-writer, `src/mux.py` the muxer, `src/defrag.py` rebuilds `moov`/`elst`. There are
deliberately **zero external binaries** for this — do not introduce ffmpeg/mp4box/Bento4 to "simplify"
a fix. ffmpeg is optional and used only for post-save ALAC integrity verification; a missing ffmpeg
is a warning, not an error. The `AppleMusicDecrypt/Dockerfile` is a separate Poetry-based path not
exercised by any CI; the documented workflow is uv. Verify it before relying on it.

## Building `wrapper/`

**For the amd-hub image, do not build it on the host.** The `Dockerfile` has a `wrapper-build`
stage that fetches NDK r23b by a checksummed URL, configures with the two flags below, and
`COPY --from=`s the launcher and the rootfs into the runtime image. `docker compose up -d
--build` is the whole procedure, and it is the only path that works from a fresh clone. The
host sequence that follows is for the **desktop** deployment and for the QEMU and Android
artifacts, which the image does not use at all.

The CMake build hardcodes the NDK toolchain path to `./android-ndk-r23b/`, which is gitignored and
**not present**. Canonical sequence, mirrored from `.github/workflows/build-lite.yml`:

```bash
cd wrapper
sudo apt-get install -y build-essential cmake unzip git aria2
sudo apt-get install -y qemu-system-x86 seabios ipxe-qemu
# NDK r23b, unzipped so that ./android-ndk-r23b/toolchains/... exists
aria2c -o android-ndk-r23b-linux.zip https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
unzip -q android-ndk-r23b-linux.zip

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)              # -> rootfs/system/bin/lite + host launchers
c++ -std=c++11 -O2 -o wrapper-lite-qemu wrapper-lite-qemu.cpp   # launcher is NOT part of CMake

apt-get download busybox-static             # required before building the initramfs
chmod +x qemu/*.sh && ./qemu/mkdata.sh && ./qemu/build.sh      # QEMU guest assets
```

Gotchas:

- **The compiler is the NDK's, not the host's.** `CMakeLists.txt:9-11` points
  `CMAKE_C_COMPILER`/`CMAKE_CXX_COMPILER` at the NDK's `x86_64-linux-android22-clang`, and both
  host launchers are built with the same `${C_COMPILER}`. So the host's gcc version is irrelevant
  and pinning the NDK pins the compiler — which is what makes the image's build reproducible
  across builder images. It also means a *newer* NDK is not a drop-in upgrade: it changes clang
  under `-Wall -Werror`, and the payload is built for a specific Android runtime.
- cJSON v1.7.19 and Dobby are pulled by CMake `FetchContent` — the first configure needs network.
  **This was the stated reason the image used to COPY prebuilt artifacts instead of building
  them, and it does not hold**: a Docker build has the network, and it is about to download a
  692 MB NDK regardless.
- `-Wall -Werror` is enabled for **both** Debug and Release. Any new warning breaks the build.
- Two flags are required and each fails differently. `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` or
  CMake ≥ 4 refuses cJSON's `cmake_minimum_required(2.8.12)` — a configure-time error.
  `-DDCURL_SHARED_LIB=<path to rootfs/system/lib64/libcurl.so>` or `find_library()` also searches
  host paths, picks a host `libcurl.so`, and the payload records the wrong SONAME and will not
  start — which reads as a library problem, not a build one. The host has that library; the
  image's builder stage does not, which is why the path is written out there.
- The build writes into the *source* tree: the `lite` target's `RUNTIME_OUTPUT_DIRECTORY` is
  `${CMAKE_SOURCE_DIR}/rootfs/system/bin`. A build can therefore configure and link and still
  write nothing, so `test_the_image_builds_the_wrapper_instead_of_copying_prebuilt_artifacts`
  asserts both outputs are executable *after* the build rather than trusting the exit code.
- `rootfs/dev/urandom` is an empty file the launcher creates itself
  (`wrapper-lite-rootless.c:108`, `O_CREAT`) and `rootfs/data/` is the launcher's own base dir.
  Neither has to exist in an image or a clone; `.dockerignore` excludes the host's, which holds a
  real logged-in account.
- `lite` links with `-Wl,--unresolved-symbols=ignore-in-object-files` against the checked-in
  `rootfs/system/lib64/*.so`. Those 100+ tracked binaries are the reason `rootfs/` is gitignored
  *except* for them — never `git clean -xfd` this repo blind.
- `BUILD_HOST_LAUNCHERS=OFF` skips the `wrapper-lite` / `wrapper-lite-rootless` chroot launchers.
  You only need the QEMU path.
- `wrapper/.gitmodules` declares a `subhook` submodule that is never initialized and is not
  referenced by any build target. An empty `git submodule status` is expected, not a broken checkout.
- Go GUI (Go 1.22+; local toolchain is 1.27): `cd gui && go build -o ../wrapper-lite-gui .`
- Android APK: JDK 17, `cd android && ./gradlew assembleDebug` — requires the QEMU assets copied
  into `android/app/src/main/assets/qemu/` first (see `android/bundle_qemu.sh`).

## `amd-hub`: the deployment

`hub/` is the only code this repository owns. `AppleMusicDecrypt/` and `wrapper/` are **pinned
submodules**: check them with `git submodule status`, move one with an explicit
`git -C <path> fetch && git checkout <sha> && git add <path>`, and **never modify anything
under them** — a local edit is invisible to `git status` at the root and is lost on the next
`submodule update`. The image builds `wrapper` itself in a builder stage, so a clone needs no
host build; see "Building `wrapper/`" below for what that stage does and does not need.

```bash
cp .env.example .env && $EDITOR .env        # AMD_PASSWORD is required
docker compose up -d --build
docker compose logs -f amd-hub

cd hub && uv run pytest -v                   # 648 tests; 33 are hub/tests/test_deployment.py
```

The Go GUI has its own, unchanged: `cd wrapper/gui && go test ./...` (`main_test.go`), and note
`process_unix.go` / `process_windows.go` split by build tag. There is still no test suite for the
C++ `lite/` sources or for the `AppleMusicDecrypt` client itself — the seam is what makes the
client testable, and `hub/tests/test_ripper_host.py` is the closest thing to covering it.

### The layout is derived, not configured — four things follow from that

`hub/hub/ripper_host.py::_VENDOR_ROOT` and `hub/hub/app.py::vendor_config_path` are both
`Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"`. There is no environment variable
for the vendor path and no argument. So with the package at `/app/hub/hub`, the client has to be
at `/app/AppleMusicDecrypt`, and:

- **`PYTHONPATH` is `/app/hub`: the directory that *contains* the package.** `import hub` needs a
  sys.path entry holding `hub/__init__.py`; the package is at `/app/hub/hub`, so that entry is
  `/app/hub`. The plan's `/app` is the *project* directory — it holds `hub/`, but as a plain
  directory with no `__init__.py`, so it can only ever yield a **namespace** package whose
  `hub.app` does not resolve. **Do not overstate the failure mode**, which I did: with the
  shipped `WORKDIR /app/hub` the plan's value *works*, because under `-m` `sys.path[0]` is the
  CWD and the CWD is already the package root. Measured in the image:

  | `PYTHONPATH` | CWD | `hub.__path__` | result |
  |---|---|---|---|
  | `/app/hub` | `/app/hub` | `['/app/hub/hub']` | regular |
  | `/app` (plan) | `/app/hub` | `['/app/hub/hub']` | regular — by accident of `WORKDIR` |
  | unset | `/app/hub` | `['/app/hub/hub']` | regular — by accident of `WORKDIR` |
  | `/app/hub` | `/` | `['/app/hub/hub']` | regular |
  | `/app` (plan) | `/` | `['/app/hub']` | **namespace** |
  | `/app` (plan) | `/app` | `['/app/hub', '/app/hub']` | **namespace**, two portions |

  So `/app/hub` is correct *independently of CWD* and `/app` is not — a `working_dir:` in
  compose or any other launch directory turns it into a namespace package. It trips the build
  gate first, which is a better place to learn than the first request, but "it currently works"
  is not a property worth relying on.

- **The client cannot be at `/opt`.** The plan's Step 1 said so; it would produce a
  `RipperHostError` naming a path that exists and is not the one the derivation wants, at every
  boot, after a build that reported success.
- **The vendor tree is deliberately absent from `PYTHONPATH`.** `src.*` must keep resolving
  through the seam's own `sys.path` insert, which is what the boundary test enforces.
- **The build context is the workspace root**, which is why the root `.dockerignore` exists and
  why it has to keep excluding `AppleMusicDecrypt/downloads/`, `hub/.venv/` and
  `wrapper/rootfs/data/` — the last holds the host's real `accounts.sqlitedb` and
  `token_cache.json` from a hand-logged-in wrapper.

**There is one command and one compose file.** The external NTFS drive used to be an optional
second root added by an overlay (`hub/deploy/compose.ntfs.yaml`), and that file was deleted
when the drive became the only library root. Two files stating the same value is two things to
keep in step, and the overlay's `${AMD_LIBRARY_ROOTS:-...}` is how 3,670 albums ended up
mounted and scanned by nothing: a default only applies when the variable is unset, and compose
reads `.env` for interpolation.

`hub/deploy/build_gate.py` asserts all of this through the code's own functions and runs as the
Dockerfile's **last** `RUN`, so a layout mistake is a build failure rather than a restart loop.
`hub/tests/test_deployment.py` holds the same invariants as tests; every one of them was
mutation-checked, and two of them caught a gap that had been written in the same session
(`user:` in compose, and a `sed` with no `grep` after it).

### The seam rule, and its two exemptions

`hub/hub/ripper_host.py` is the only module allowed to reach the vendor tree, and
`tests/test_ripper_host.py` walks the AST of `hub/hub/`, `hub/spike/` **and `hub/deploy/`**,
failing on any `import src.*`, on `importlib`/`runpy`, on `__import__`, and on any reference to
`sys.path`. There is one other exemption, `hub/hub/vendor.py`, which is a deliberate pass-through
to a single `AppleMusicURL.parse_url` so that a second copy of the URL grammar cannot drift —
keep it a pass-through, and do not widen it.

### Review-focus hazards, worst first

- **`AMD_WRAPPER_BINARY` must be `wrapper-lite-rootless`, not the QEMU launcher.** The QEMU one
  has no host rootfs and passes `--base-dir` *into the guest*, so the 2FA code the hub writes is
  in a namespace the hub cannot see; `submit_2fa` refuses by design rather than writing a file
  nobody will read. `config.py`'s `DEFAULT_WRAPPER_BINARY` is still the QEMU one and is correct
  for the upstream desktop deployment, so the **image** overrides it.
- **`<vendor>/config.toml` is the only path the client opens.** The image builds it there from
  upstream's `config.example.toml` (three `sed`s, each asserted) rather than forking a copy.
  `dirPathFormat` is rewritten **absolute** and to `AMD_DOWNLOAD_ROOT`, because the seam
  `chdir`s into the vendor root: left relative, the client writes to
  `AppleMusicDecrypt/downloads/` while the scan reads the bind mount, and every track
  re-downloads forever with nothing red.
- **Both `security_opt` values are mandatory** and `cap_add` must stay absent —
  `seccomp:unconfined` alone gives `mount proc failed: EPERM`, and `cap_add: [SYS_ADMIN]` was a
  *control experiment* proving it cannot help (spike §5.5). `systempaths=unconfined` is a real
  hardening reduction (writable `/proc/sys` for a root process, `sysrq` + `sysrq-trigger`
  together giving reboot/crash) and spec §14.1 records it as a deliberate, bounded trade. Keep
  the honesty in the comments; do not quietly ship it.
- **No `user:` in compose.** The launcher `unshare(CLONE_NEWUSER)`s with a single-uid map
  (`0 0 1`) before touching the filesystem, so a non-root container cannot write a root-owned
  rootfs even with `CAP_DAC_OVERRIDE` (spike §5.1 F2).
- **No `network_mode: host`.** The launcher does not unshare a *network* namespace, so `host`
  would put its bind on the host's loopback, where the operator's own wrapper QEMU already
  listens — and a failed bind makes the payload signal *itself*, logging a line identical to an
  external kill (spike §6.5).
- **One process, and it is not reachable from compose.** `--workers 1` lives in
  `hub.app.main()`. Everything the app owns is on `app.state`, so a second worker is a second
  broker, job store, leaf registry, scheduler and session generation.
- **Ripping is a worker pool, and three things about it are not obvious.** `AMD_RIP_CONCURRENCY`
  (default 4) workers, each looping claim-run-mark, because 85 % of a track's wall clock is the
  wrapper's metadata round-trip — a 41.8 MB ALAC track is 9.8 s of which 6.1 s is pre-audio. The
  hazards, worst first:

  1. **A declined row must be *excluded from the claim*, not put back.** `rip.py:166` guards on
     `get_task(url.id)` — `adam_id` alone, no codec — so the same track queued as `alac` and
     `aac` must be ripped one at a time or the second returns early and is marked `done` having
     downloaded nothing. The queue's own index is `(adam_id, codec)`, so both rows are legal and
     the hub has to decline one. Putting it back makes it the lowest eligible id again, and the
     next claim returns it — and the claim loop is **synchronous end to end**, so that is not a
     slow queue, it is an event loop that stops: no requests, no progress stream, no `docker
     stop`. `claim_next(exclude=...)` plus a `job.id in declined` exit make it unreachable twice.
     `test_no_job_is_claimed_twice_in_one_pass` asserts the invariant but **cannot** catch a
     spin — a spin hangs rather than fails, which is the reason for two guards and not one.
  2. **The pool collects, then re-raises — and what it re-raises is not only `CancelledError`.**
     Plain `gather` propagates the first child exception to its awaiter and leaves the siblings
     running as *orphans*; it does not cancel them. The per-job `except Exception` covers a rip,
     but not `claim_next`/`mark`, which are synchronous and unwrapped — and four workers on one
     SQLite file is four times the chance of `database is locked` at `busy_timeout`. Collecting
     first settles the workers holding jobs; re-raising a *settled* pool's error is the only
     shape that reports the problem without stranding rows.
  3. **A failure no longer stops the queue.** Serial, a job that parked left the rest of the
     pass untouched. Pooled, everything claimed in that pass is attempted. That is deliberate —
     one bad track must not cost three good ones — but it means "the loop must not run the next
     job past a blocker" is no longer a property to assert, and `test_a_parked_job_keeps_its_
     place_in_the_queue` was rewritten rather than left passing by accident.

  `run_one` is `run_pool` with a budget of one, so a ceiling of 1 is a *sequential queue*, not
  "one job per call" — it still drains. Asserting a count there tests the wrong thing; the peak
  is the promise the setting makes.
- **The Apple token DB is not on `/data`.** The launcher chroots *before* resolving `--base-dir`,
  so `/data/wrapper` means `<rootfs>/data/wrapper` inside the chroot. It needs the
  `wrapper-data` volume at `/opt/wrapper/rootfs/data` or the Apple account dies on every
  `down`/`up`. (Observed: with it missing the launcher logged `mkdir base_dir_arg failed` —
  `perror` at `wrapper-lite-rootless.c:143`, reporting the `mkdir` on line 142.)
- **Boot time depends on whether the wrapper has an Apple account, and the number usually quoted
  is the slow one.** Uvicorn binds only after the lifespan, whose first step blocks on
  `startup_timeout`, so the port is closed until the wrapper is ready. With **no account**,
  `WrapperSupervisor._wait_ready` polls out the whole 60 s and *then* raises "no account" —
  measured 65, 65, 66 and 73 s over four runs. With an **account logged in**, readiness needs
  only non-empty `regions` and the wrapper is serving in seconds: measured 6 s to
  `wrapper-lite listening`, ~20 s to the first `/api/health` 200. Quote the range you measured,
  and say which case it is — "first boot takes 65 s" is wrong for every boot after the first login.
  `start_period: 120s` is arithmetic, not taste: the budget is derived from `startup_timeout` by
  `test_the_healthcheck_budget_covers_the_wrapper_startup_timeout` rather than transcribed, and it
  has to cover the no-account case. Lower it and `restart: unless-stopped` turns a correct boot
  into a crash loop.
- **An open SSE connection delays shutdown.** The queue page holds an `EventSource`, so uvicorn
  waits for it to drain; `docker stop` gives 10 s and then SIGKILLs, and the container can sit in
  "Waiting for connections to close" long past that. Close browser tabs on `/queue` before
  restarting, or expect a slow `docker compose up -d`.
- **The 2FA file is written into the launcher's rootfs, mode 0600, and the child removes it.**
  Do not "improve" that mechanism.
- **Apple credentials are a deliberate exposure, not an accident.** They reach the wrapper on a
  login child's argv (`--login user:pass`) because argv is the binary's only input, so
  `/proc/<pid>/cmdline` is readable by a same-uid process. Bounded by topology: one service
  process in one container. Never write them to the image, compose, or any `ENV`.

### Library roots

**There is one, and which host directory backs it is the operator's.**
`AMD_LIBRARY_HOST` in `.env` names it — any directory, required, with no default, because a
wrong guess is an empty library that reads as healthy. On the machine this was written on it
happens to be an external NTFS volume mounted at `/home/m/Music/HDD_Music`; that is a choice,
not a requirement, and a machine with only an internal disk runs the same compose file
unchanged. The container path is `/library`, and it is simultaneously the scan root and the
download target — `AMD_DOWNLOAD_ROOT` in the Dockerfile feeds both the baked `dirPathFormat`
and `ENV AMD_LIBRARY_ROOTS`, so they cannot be set apart. Compose's own three spellings of
that path cannot derive from a build arg, so
`test_the_container_side_library_path_is_the_same_value_everywhere_it_appears` is what ties
them together. The client's own `downloads/` tree is no longer mounted at all.

`create_host_path: false` on that bind is load-bearing. Compose's short syntax creates a
missing host path, and Docker resolves a symlink source first, so with the directory absent the
stack would start against a directory Docker had just mkdir'd, mount it as an
empty library, and re-download every album with no error anywhere. An empty root reads as
healthy: `degraded_roots` covers a root that cannot be *read*, not one that was never populated.

`library_scan` computes relpaths against
the root **exactly as the caller passed it** and never resolves it — a relpath is stored, quoted
into `skip_reason`, and joined back onto the same root string, so resolving it would be a bug.
The compose file therefore asks the operator for a **path they manage**, rather than naming the
`/run/media/<UUID>/` automount target a volume would mount at: the UUID path is udev's and rots
when the volume is reformatted or unplugged.

A root that is *unreadable* is reported in `degraded_roots`. A root that is a **silently empty
mount point** is not — `library_scan` has no way to tell an empty library from an absent drive —
which is why the bind refuses to be created rather than starting up against an empty one.
`/api/status` carries a `per_root` count for the same reason, and
`hub/deploy/acceptance_check.py` reports **per-root** album counts: a plausible total is
equally consistent with a full library and with an empty mount.

## Environment facts (verified on this machine)

- `AppleMusicDecrypt/.venv` is Python **3.13.7**; system `python3` is 3.14.7. Use `uv run` or
  `.venv/bin/python` — never bare `python3`.
- `temari` ships per-platform cdylibs; `utils.check_dep()` loads it eagerly at startup and prints the
  selected platform key on failure. A Temari import error is a platform-mismatch symptom, not a
  missing dependency — re-run `uv sync` before debugging further.
- `AppleMusicDecrypt/downloads/` is **69 GB** and gitignored. Don't walk, grep, or commit it.
- `ffmpeg` and `cmake` are on PATH; `/dev/kvm` exists. Go 1.27 is installed.
- `AppleMusicDecrypt/requirements.txt` is generated — its header records the exact command:
  `uv export --format requirements-txt --no-hashes --no-dev --no-emit-project -o requirements.txt`.
  Edit `pyproject.toml` / `uv.lock` and re-export; never hand-edit it.
