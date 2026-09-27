# amd-hub

A web UI over the [AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt)
client and the `wrapper-lite` backend, packaged as one container. It watches your music library,
deduplicates against it, and drives the downloader so an album is requested from a browser
instead of a terminal.

## Run it

```bash
git clone --recurse-submodules <this repo> amdl_extend
cd amdl_extend

cp .env.example .env
$EDITOR .env                      # two required values, see below
docker compose up -d --build
docker compose logs -f amd-hub
```

`.env` needs **two** values, and `docker compose up` stops with a message naming whichever one
is missing:

| Variable | What it is |
|---|---|
| `AMD_PASSWORD` | The web UI's single shared password. Any string. |
| `AMD_LIBRARY_HOST` | The absolute path of a directory holding your music. Any directory — it does not have to be a separate drive, and it does not have to be NTFS. |

`--recurse-submodules` is not optional. `AppleMusicDecrypt/` and `wrapper/` are submodules
pinned to a commit, and the image is built from both — the Dockerfile has a stage that compiles
the wrapper's launcher and payload with the Android NDK, so there is no host build to do and no
prebuilt binary to go looking for. A clone without the submodules fails at the first `COPY`.

The first build downloads the NDK (692 MB, one layer, cached afterwards) and fetches cJSON and
Dobby for CMake, so allow a few minutes on a cold cache and expect the network to be the slow
part. Later builds reuse the layer and do not.

Then open <http://localhost:8080/>. The first boot takes about a minute and a half before the
port answers — see [Why the first boot is slow](#why-the-first-boot-is-slow).

To check the library directory before starting, read the value back out of `.env` rather than
expecting your shell to have it — compose consumes that file, the shell does not:

```bash
. ./.env && ls -d "$AMD_LIBRARY_HOST" && echo "library found"
```

Stop it with `docker compose down`. Add `-v` to forget the Apple login and the queue.

## Logging in to Apple

The hub's own password (`AMD_PASSWORD`) gets you into the web UI. The **Apple account** is
separate, and this is the part worth reading twice:

1. Open the UI and log in with `AMD_PASSWORD`.
2. `POST /api/wrapper/login`, or the wrapper panel in the UI, with your Apple ID and password.
   They go straight to the wrapper's login process and are held nowhere else — not in the
   environment, not in the database, not in the log pane.
3. If the account uses 2FA, the wrapper asks for a code. The hub notices, and
   `POST /api/wrapper/login/2fa` takes it. The code is written to
   `/opt/wrapper/rootfs/data/wrapper/2fa.txt` inside the launcher's own filesystem, mode 0600,
   and the wrapper deletes it once it has read it.
4. Restart the wrapper (`POST /api/wrapper/restart`). It comes up with your account, `regions`
   populates, and downloads can start.

**The Apple account is stored in a Docker volume, not in the image.** Deleting the volume
(`docker compose down -v`) is how you make the hub ask again.

### Why your Apple password is on a process's command line

The wrapper takes credentials in exactly one way: `--login user:pass` on its command line. There
is no stdin path, no socket, no file. So for as long as the login process lives, its arguments
are readable via `/proc/<pid>/cmdline` by any process running as the same user.

This is a deliberate, recorded trade rather than an oversight, and what bounds it is
the shape of the deployment: the container runs a single service process as one user, so the only
readers of that command line are the hub itself and the login process, and the login process
lives for seconds. It is not protection on a multi-tenant host.

## What is exposed

One port, `8080`, on your LAN. The wrapper's own API (port 12340) is unauthenticated and serves
decrypted audio, so it is bound to the container's loopback and **never published**.

The container is not `privileged` and adds no capabilities. It does run as root, which the
rootless launcher requires: it creates a user namespace with a single-UID map before touching its
own filesystem, so a non-root container cannot write a root-owned rootfs.

One `security_opt` value is a real reduction in hardening and is worth stating plainly.
`systempaths=unconfined` removes Docker's masking of `/proc`, so inside this container a root
process can write `/proc/sys` kernel tunables — including `/proc/sys/kernel/sysrq`, which
together with the now-writable `/proc/sysrq-trigger` gives `reboot` and `crash`. Without it the
container cannot start the wrapper at all: Docker over-mounts twelve paths under `/proc`, the
launcher mounts its own `procfs` in its user namespace, and the kernel refuses with
`mount proc failed: Operation not permitted`. Adding `SYS_ADMIN` does not help — that was a
control experiment, and it failed the same way.

## The library root

There is one root, and it is the same tree for both halves of the job: the client downloads into
it and the hub scans it before every download, so a track already on disk is skipped and
`skip_reason` names the exact paths that matched. It is mounted at `/library` inside the
container, and `AMD_LIBRARY_HOST` in `.env` says which host directory that is.

Those two are the same mount, so they cannot be set apart — `AMD_LIBRARY_HOST` on the host,
`AMD_LIBRARY_ROOTS` in the container — and compose refuses to create the host path, so a
directory that does not exist is a failed start naming the variable rather than an empty library.
Check it exists before the first start: a root that cannot be *read* is reported as degraded, but
a root that is a silently empty mount point is not, and the symptom would be a quiet
re-download of everything that lived there.

## Configuration

All of it is in `.env`; see `.env.example` for the annotated list. The ones worth knowing about:

| Variable | Default | |
|---|---|---|
| `AMD_PASSWORD` | — | **Required.** The web UI's single shared password. |
| `AMD_LIBRARY_HOST` | — | **Required.** The host directory holding your music. Any directory; no default, because a wrong guess is an empty library that reads as healthy. |
| `AMD_SESSION_SECRET` | generated per process | Set it (32+ chars) to stay logged in across restarts. |
| `AMD_RIP_CONCURRENCY` | `4` | How many tracks to rip at once. `1` restores one-at-a-time. |

Running the hub outside compose needs both required variables in the environment —
`AMD_PASSWORD` and `AMD_LIBRARY_ROOTS`; the app does not read `.env`, which compose alone
consumes.

Everything else — the wrapper binary, its base directory, the database path, the bind address —
is an `ENV` in the `Dockerfile`, on purpose: those values have to agree with the config file
inside the image, and the build asserts that they do. Changing one means changing it in one
place.

Upstream's own ~150 settings are read from `/app/AppleMusicDecrypt/config.toml`, which the image
builds from upstream's `config.example.toml`. To change one, edit that file in the image and
rebuild — there is no environment variable for it, because upstream's config loader has none.
`region.language` is the exception: it is a build ARG, so `AMD_VENDOR_LANGUAGE` in `.env` plus a
rebuild is enough.

## Why the first boot is slow

About 65 to 75 seconds, and it is not a bug. The wrapper takes 12–19 seconds to start serving, and the
hub will not declare it ready until `/status` reports a non-empty `regions` list — because an
instance with no regions cannot download anything. On a host with no Apple account logged in, that
means the hub waits out its full 60-second startup budget and then tells you so precisely:

> no account is logged in on the wrapper at http://127.0.0.1:12340/status: it is up and
> answering /status, but regions is empty, so it cannot serve a download.

That is a login prompt, not a failure. Log in as above and restart the wrapper. The container's
`start_period` is 120 s for exactly this reason; lowering it makes a correct boot look like a
crash loop.

## Development

```bash
cd hub && uv run pytest -v          # 648 tests
```

`hub/` is the only code here we own. `AppleMusicDecrypt/` and `wrapper/` are separate upstream
clones, left untouched.

`hub/tests/test_deployment.py` holds the deployment's invariants — the vendor-path derivation,
`PYTHONPATH`, the config pin, the security posture, the mounts. `hub/deploy/build_gate.py` runs
as the image's last build step and asserts the same layout from inside the image.
`hub/deploy/acceptance_check.py` checks the library claims against the real mounted tree:

```bash
docker compose cp hub/deploy/acceptance_check.py amd-hub:/tmp/acceptance_check.py
docker compose exec -T amd-hub /opt/venv/bin/python /tmp/acceptance_check.py
```

Use `docker compose cp`, not `docker cp` — Compose v2 names the container
`<project>-<service>-<index>`, so `docker cp … amd-hub:` fails with `No such container`.

The library page also carries a **per-root album count**, and that is not decoration. A drive
that is not plugged in is not necessarily *missing*: Docker's bind-mount autocreate makes the
directory, so the root arrives mounted, readable, and empty. Nothing reports that as a problem,
and the symptom is a quiet re-download of everything that lived on the drive. The count is what
makes a zero visible.

## Licence and provenance

`hub/` is the work of this repository. `AppleMusicDecrypt/` and `wrapper/` are upstream projects
with their own licences, cloned unmodified.
