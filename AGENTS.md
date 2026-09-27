# AGENTS.md — amdl_extend

Workspace notes for agents. General engineering rules come from the global
`~/.config/opencode/AGENTS.md` and still apply. This file records repo-specific
facts that are hard to infer from the code.

## Layout: one repo, two pinned submodules

| Path | Gitlink | Upstream | Branch |
|------|---------|----------|--------|
| `AppleMusicDecrypt/` | `8b609df` | `WorldObservationLog/AppleMusicDecrypt` | `v3` |
| `wrapper/` | `c61dea9` | `itouakirai/wrapper` | `lite` |

**Cloning needs `--recurse-submodules`.** Moving a pin is a deliberate act:
`wrapper`'s `rootfs/` is 101 tracked `.so` files, so a re-pin changes the payload.

Their own `.gitignore` files still apply inside them, and the root's does not.

## Always `cd AppleMusicDecrypt` before running anything

Three paths are resolved **relative to the current working directory**, and
`main.py` never chdirs:

- `config.toml` — `Config.load_from_config()` default arg
- `assets/prefetch_template.json` — `EMBEDDED_TEMPLATE_PATH`
- `downloads/` — default `dirPathFormat`

Launching from the workspace root silently skips the embedded FairPlay prefetch
template and starts making `/key` RPCs it doesn't need to.

## Commands

```bash
cd AppleMusicDecrypt
uv sync                          # setup
uv run python main.py            # full-screen TUI (default)
uv run python main.py --legacy-ui   # v2-style plain REPL
```

No Python test suite. The only automated check is:

```bash
.venv/bin/python -m compileall -q src main.py
```

## The two repos are not wired together

The client needs a wrapper HTTP API at `[instance] url` (default `127.0.0.1:12340`).
**Login happens wrapper-side, not in this client.**

- `[localInstance] wrapperType` defaults to `"manager"` → expects `wrapper-manager-qemu`
  from a repo that is *not* cloned here.
- The local `wrapper/` clone builds **`wrapper-lite-qemu`**, i.e. the `"lite"` backend.
- To run the local clone: set `wrapperType = "lite"`, build `wrapper-lite-qemu`,
  and point `launcherBin` at the resulting binary.

## Stale `__pycache__` will silently lie

CPython validates a `.pyc` by source mtime+size, so a source file rewritten
within the same size and second keeps its old bytecode. Before believing a
failure in `hub/`, run `find hub -name __pycache__ -type d -exec rm -rf {} +`
and re-run.

## `hub/` invariants

- **`normalize()` is applied to both sides of every comparison**, and the
  album-name path uses `strip_track_prefix=False`. If the index build and the
  lookup disagree, every album lookup misses and nothing is ever skipped.
- **`dedup.find_duplicate` is a pure function** over a `LibraryScan`. It must
  not grow a cache, a filesystem read, or an index table.
- **`RipperHost` holds the process working directory.** Every path `hub/` uses
  is therefore absolute — a relative path added later will resolve against
  `AppleMusicDecrypt/` and fail silently on reads.

Run tests: `cd hub && uv run pytest -v`

## Config changes touch three places

1. Add the field to the matching model in `src/config.py` with a default.
2. Add it to the same `[section]` in `config.example.toml` with its comment.
3. Bump `CONFIG_VERSION` in *both* files. They must match.

`.github/workflows/win-build.yml` string-replaces the **literal lines**
`launcherBin = ""` and `enable = false` in `config.example.toml`. Reformatting
or renaming those lines silently breaks the Windows artifact.

## Client architecture

- **DI is `creart`.** Creators are registered in `main.py` in strict dependency
  order. Adding a singleton means adding an `AbstractCreator` subclass *and* an
  `add_creator` call there.
- **TUI is prompt_toolkit**, not Textual. Never call `os._exit` — use
  `app.exit()` (TUI) or `_LegacyExit` (legacy REPL).
- **Two decryption paths:** FairPlay (`temari` Rust cdylib) for most content,
  Widevine (`src/legacy/` + pywidevine) for `aac-legacy` and music videos.
- **Container handling is hand-written** (`src/mp4.py`, `src/mux.py`,
  `src/defrag.py`). Zero external binaries — do not introduce ffmpeg/mp4box.

## Building `wrapper/`

**For the amd-hub image, do not build on the host.** The Dockerfile has a
`wrapper-build` stage. `docker compose up -d --build` is the whole procedure.

The CMake build hardcodes the NDK toolchain path to `./android-ndk-r23b/`.
Canonical sequence (mirrored from `.github/workflows/build-lite.yml`):

```bash
cd wrapper
sudo apt-get install -y build-essential cmake unzip git aria2 qemu-system-x86 seabios ipxe-qemu
aria2c -o android-ndk-r23b-linux.zip https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
unzip -q android-ndk-r23b-linux.zip
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
c++ -std=c++11 -O2 -o wrapper-lite-qemu wrapper-lite-qemu.cpp
apt-get download busybox-static
chmod +x qemu/*.sh && ./qemu/mkdata.sh && ./qemu/build.sh
```

Gotchas:
- `-Wall -Werror` for both Debug and Release. Any new warning breaks the build.
- Two flags required: `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` and
  `-DDCURL_SHARED_LIB=<path to rootfs/system/lib64/libcurl.so>`.
- The build writes into the *source* tree (`rootfs/system/bin`).
- `lite` links with `-Wl,--unresolved-symbols=ignore-in-object-files` against
  the checked-in `rootfs/system/lib64/*.so`. Never `git clean -xfd` this repo.

## `amd-hub`: the deployment

`hub/` is the only code this repository owns. **Never modify anything under
the submodules** — a local edit is invisible to `git status` at the root.

```bash
cp .env.example .env && $EDITOR .env        # AMD_PASSWORD is required
docker compose up -d --build
docker compose logs -f amd-hub
cd hub && uv run pytest -v                   # 648 tests
```

### Key invariants

- **`PYTHONPATH` is `/app/hub`** (the directory that *contains* the package).
- **No `user:` in compose.** The launcher `unshare(CLONE_NEWUSER)`s with a
  single-uid map, so a non-root container cannot write a root-owned rootfs.
- **No `network_mode: host`.** The launcher does not unshare a network
  namespace, so `host` would put its bind on the host's loopback.
- **One process.** `--workers 1` lives in `hub.app.main()`.
- **Both `security_opt` values are mandatory** and `cap_add` must stay absent.
- **The Apple token DB is not on `/data`.** The launcher chroots *before*
  resolving `--base-dir`, so it needs the `wrapper-data` volume at
  `/opt/wrapper/rootfs/data`.
- **Boot time:** ~65–75 s with no Apple account, ~20 s with one logged in.
  `start_period: 120s` covers the no-account case.
- **An open SSE connection delays shutdown.** Close browser tabs on `/queue`
  before restarting.
- **Apple credentials are a deliberate exposure.** They reach the wrapper on a
  login child's argv. Never write them to the image, compose, or any `ENV`.

### Library roots

**There is one, and which host directory backs it is the operator's.**
`AMD_LIBRARY_HOST` in `.env` names it — any directory, required, with no default.

`create_host_path: false` on that bind is load-bearing. Compose's short syntax
creates a missing host path, so a directory that does not exist is a failed
start naming the variable rather than an empty library.

### The seam rule

`hub/hub/ripper_host.py` is the only module allowed to reach the vendor tree.
`tests/test_ripper_host.py` walks the AST of `hub/hub/` and `hub/deploy/`,
failing on any `import src.*`, on `importlib`/`runpy`, on `__import__`, and on
any reference to `sys.path`. One exemption: `hub/hub/vendor.py`, a deliberate
pass-through to a single `AppleMusicURL.parse_url`.

## Environment facts

- `AppleMusicDecrypt/.venv` is Python **3.13.7**; system `python3` is 3.14.7.
  Use `uv run` or `.venv/bin/python` — never bare `python3`.
- `AppleMusicDecrypt/downloads/` is **69 GB** and gitignored. Don't walk, grep, or commit it.
- `ffmpeg` and `cmake` are on PATH; `/dev/kvm` exists. Go 1.27 is installed.
- `AppleMusicDecrypt/requirements.txt` is generated — edit `pyproject.toml` /
  `uv.lock` and re-export; never hand-edit it.
