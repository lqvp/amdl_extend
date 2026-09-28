# AGENTS.md — amdl_extend

Workspace notes for agents. General engineering rules come from the global
`~/.config/opencode/AGENTS.md` and still apply. This file records repo-specific
facts that are hard to infer from the code.

## Layout: this repo, plus two upstream trees the build clones

| Upstream | Pin | Branch |
|---|---|---|
| `WorldObservationLog/AppleMusicDecrypt` | `8b609df` | `v3` |
| `itouakirai/wrapper` | `c61dea9` | `lite` |

**Neither tree is in this repository.** No `.gitmodules`, no gitlink, no
`git submodule status`: the Dockerfile clones both at build time, pinned by the
`VENDOR_COMMIT` and `WRAPPER_COMMIT` args. A plain `git clone` of this repo is
therefore enough to build the image, and `.gitignore` and `.dockerignore` both
exclude `AppleMusicDecrypt/` and `wrapper/` as whole trees so a local checkout
cannot enter the build context.

Moving a pin is still deliberate: `wrapper`'s `rootfs/` is 101 tracked `.so`
files, so a re-pin changes the payload.

## The vendor tree: where it is, and what is CWD-relative

The client is cloned to `/app/AppleMusicDecrypt` — derived, not configured, by
`parents[2]` of the installed package. So three paths are resolved **relative to
the current working directory**, and `main.py` never chdirs:

- `config.toml` — `Config.load_from_config()` default arg
- `assets/prefetch_template.json` — `EMBEDDED_TEMPLATE_PATH`
- `downloads/` — default `dirPathFormat`

`RipperHost` holds the process CWD at the vendor root for its whole life, which
is the only reason the image's layout is load-bearing. Two consequences:

- `config.toml` must be built *at* the vendor root, and the image builds it there
  from upstream's `config.example.toml`. A config anywhere else is not a
  different config — it is one that is silently ignored while the image's own is
  used instead.
- Launching the client from anywhere else silently skips the embedded FairPlay
  prefetch template and starts making `/key` RPCs it doesn't need to.

There is no host checkout to run the client against, so upstream's own
`uv sync` / `uv run python main.py` are not part of this repository's workflow.
The image is the only supported way to run it.

**Login happens wrapper-side, not in this client.** The client only ever talks
to a wrapper HTTP API at `[instance] url`. The image's `AMD_WRAPPER_BINARY` names
`wrapper-lite-rootless`, not `wrapper-lite-qemu`, because the QEMU build has no
host rootfs and cannot serve the 2FA code the hub writes.

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

**Do not build the wrapper on the host.** The Dockerfile has a `wrapper-build`
stage and `docker compose up -d --build` is the whole procedure.

The two CMake flags and the NDK pin are documented at the instructions that use
them, and `hub/tests/test_deployment.py` asserts all three — the flags because
each alone is a build that fails later and more confusingly, the pin because a
version that moved would rebuild the payload with a different clang under
`-Wall -Werror`. `NDK_VERSION` is spelled as upstream's Dockerfile spells it and
is deliberately **not** a value to raise; the test asserts 23 and says why.

## `amd-hub`: the deployment

`hub/` is the only code this repository owns. The two upstream trees are not
here at all: a local `AppleMusicDecrypt/` or `wrapper/` is an untracked working
copy that the build ignores, so an edit made there cannot reach the image and
`git status` will not report it.

```bash
cp .env.example .env && $EDITOR .env        # AMD_PASSWORD is required
docker compose up -d --build
docker compose logs -f amd-hub
cd hub && uv run pytest -v                   # 647 tests
cd hub-go && go build ./... && go test ./...  # the hub itself
```

### Key invariants

- **`PYTHONPATH` is `/app/hub`** (the directory that *contains* the package).
- **No `user:` in compose.** The launcher `unshare(CLONE_NEWUSER)`s with a
  single-uid map, so a non-root container cannot write a root-owned rootfs.
- **No `network_mode: host`.** The launcher does not unshare a network
  namespace, so `host` would put its bind on the host's loopback.
- **One process, and there is no knob for it.** The container runs one binary
  (`/usr/local/bin/amdhub`); `hub-go/cmd/amdhub/main.go` takes no flags and reads
  everything from the environment. (The Python hub still passes `workers=1` inside
  `hub.app.main()`; it is no longer the entry point.)
- **The hub is Go; the client is Python.** `hub-go/` owns everything except the Apple
  client, which is reached across a line-delimited JSON pipe by
  `hub-go/tools/pyworker.py`; do not reimplement the client or the vendor seam.
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

- `hub/` is a `uv` project and the image pins `python:3.13-slim`. Use `uv run pytest`
  — never bare `python3`, whose interpreter is a different minor version.
- `ffmpeg` and `cmake` are on PATH; `/dev/kvm` exists. Go 1.27 is installed.
- A local `AppleMusicDecrypt/downloads/` is tens of GB and gitignored. Don't walk it,
  grep it, or be surprised when `du` is slow.
