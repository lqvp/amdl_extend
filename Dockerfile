# syntax=docker/dockerfile:1

# **The download root and the library roots are the same directory, and they are decided
# here.** `dirPathFormat` is baked into `<vendor>/config.toml` at build time while
# `AMD_LIBRARY_ROOTS` is read at runtime, so when they were two independent literals a
# `.env` could point the scan at a tree the client never writes into -- and every track
# re-downloads for ever, with no error anywhere. One argument feeds both, so the
# arrangement that breaks it cannot be expressed.
#
# The value is a path *inside* the container. The host side of the bind mount is compose's
# business, and it is set from the same name in the same place.
ARG AMD_DOWNLOAD_ROOT=/library
#
# amd-hub runtime image (spec §3, §3.2, §4, §11; task 10 of the phase-1 plan).
#
# Build from the WORKSPACE ROOT -- the context has to contain both upstream clones:
#
#   docker build -t amd-hub:phase1 .        # or, equivalently:
#   docker compose build
#
# The context is the workspace root because of a derivation in the code, not for
# convenience. `hub/hub/ripper_host.py::_VENDOR_ROOT` and
# `hub/hub/app.py::vendor_config_path` are both
# `Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"`. With the package installed at
# /app/hub/hub, parents[2] is /app, so the client has to be at /app/AppleMusicDecrypt for
# the derivation to hold. Move either tree and the failure is not a "file not found" you can
# read -- it is a `RipperHostError` naming a path that exists and is not the one it wanted,
# at every boot, after the build already succeeded. `hub/deploy/build_gate.py` asserts the
# layout through the code's own functions so that lands on the build, not on the first start.
#
# ---------------------------------------------------------------------------
# Stage 1: build the wrapper launcher and its payload from source
# ---------------------------------------------------------------------------
# The plan's stage 1, and it was left out for a while for two reasons. One held: the build
# needs the network, because CMake `FetchContent` pulls cJSON v1.7.19 and Dobby at *configure*
# time. The other did not, and had quietly become the reason the image was unpinnable. A
# Docker build has the network -- it is about to download a 692 MB NDK -- so a configure that
# fetches two git repositories is not an obstacle. What that arrangement actually bought was a
# binary nobody could rebuild: `wrapper/wrapper-lite-rootless` and
# `rootfs/system/bin/lite` are both gitignored upstream build outputs, `wrapper/rootfs/` is
# 101 tracked .so files plus exactly one compiled binary, and a fresh clone therefore had no
# way to produce the image. `test_the_wrapper_artifacts_are_gitignored_so_a_fresh_clone_cannot_build`
# recorded that as intentional, which made it deliberate rather than accidental.
#
# The compiler is not the host's. `CMakeLists.txt:9-11` points `CMAKE_C_COMPILER` and friends at
# `${ANDROID_NDK_PATH}/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android22-clang`,
# and the host launchers are built with the same `${C_COMPILER}`, so `wrapper-lite-rootless` is
# an NDK-clang output too. That is worth more than it sounds: `-Wall -Werror` is on for both
# Debug and Release, so a *different* compiler would mean a *different* build, and any warning
# it invented would break the image. Pinning the NDK pins the compiler, which makes the stage
# reproducible on any machine regardless of what gcc its base image ships.
#
# `busybox-static`, `qemu-system-x86`, `seabios` and `ipxe-qemu` are deliberately absent. The
# image runs `wrapper-lite-rootless`, not `wrapper-lite-qemu`, so there is no guest kernel and
# no initramfs to assemble and `qemu/mkdata.sh` / `qemu/build.sh` are never invoked.

FROM debian:bookworm-slim AS wrapper-build

# git is not optional: `FetchContent` clones cJSON and Dobby. `unzip` unpacks the NDK.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates curl unzip; \
    rm -rf /var/lib/apt/lists/*

# The NDK is pinned twice: by version in the URL, and by the SHA-256 of Google's own bytes.
# The checksum is not the one from a local copy that happened to work -- it is the digest of a
# fresh download from dl.google.com, so the pin cannot inherit a corrupted local artifact.
# Verified against the same host file by SHA-1, which matches, so the host build and this
# stage compile with an identical toolchain.
ARG ANDROID_NDK_URL=https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
ARG ANDROID_NDK_SHA256=c6e97f9c8cfe5b7be0a9e6c15af8e7a179475b7ded23e2d1c1fa0945d6fb4382

WORKDIR /src/wrapper
# The context excludes `wrapper/build/`, `wrapper/android-ndk-r23b/` and
# `wrapper/rootfs/data/`, so this is the tracked tree (the 101 .so files the payload needs and
# the one the libcurl pin below resolves) and nothing else -- no 2.3 GB of NDK, no host
# account database.
COPY wrapper/ /src/wrapper/

RUN set -eux; \
    curl -fsSL -o ndk.zip "$ANDROID_NDK_URL"; \
    echo "$ANDROID_NDK_SHA256  ndk.zip" | sha256sum -c -; \
    unzip -q ndk.zip; \
    rm ndk.zip

# Both flags are from docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md §2.1,
# and either one alone is a build that fails later and more confusingly:
#
#   -DCMAKE_POLICY_VERSION_MINIMUM=3.5   CMake >= 4 refuses cJSON's cmake_minimum_required(2.8.12).
#   -DDCURL_SHARED_LIB=<path>             find_library() also searches host paths and would pick up
#                                         a /usr/lib/libcurl.so; the Android payload then records
#                                         the wrong SONAME and will not start. A host has that
#                                         library and this stage does not, which is why the path
#                                         is written out rather than left to be found.
#
# `BUILD_HOST_LAUNCHERS=ON` is the default and is stated because turning it off silently
# produces an image with a rootfs and no launcher, which fails at boot rather than at build.
RUN set -eux; \
    cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DBUILD_HOST_LAUNCHERS=ON \
        -DDCURL_SHARED_LIB=/src/wrapper/rootfs/system/lib64/libcurl.so; \
    cmake --build build -j"$(nproc)"; \
    test -x ./wrapper-lite-rootless; \
    test -x ./rootfs/system/bin/lite

# ---------------------------------------------------------------------------
# Stage 2: the runtime image
# ---------------------------------------------------------------------------
FROM python:3.13-slim

# ca-certificates: the client talks to the Apple API and CDN over TLS. The base image has it,
# and it is named here so that "the image works" cannot depend on that staying true.
# curl: compose's healthcheck target, and the first thing anybody debugs with.
# ffmpeg: Phase 3's post-save ALAC integrity check. Upstream treats a missing ffmpeg as a
# warning rather than an error, so this is a convenience today and a requirement later --
# but it is cheap now and expensive to retrofit, which is the plan's reason for including it.
# uv: build-time only. The venv is at /opt/venv and CMD calls its python directly, so nothing
# re-resolves the lock at container start.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        curl \
        ffmpeg \
    && pip install --no-cache-dir uv==0.12.9 \
    && rm -rf /var/lib/apt/lists/*

# Ordered by how rarely each input changes, so an edit to the hub does not invalidate the
# 121 MB wrapper layer below it (spec §3.2). --frozen refuses to re-resolve, so the image is
# built from uv.lock rather than from whatever PyPI serves on the day; --no-install-project
# defers `amd-hub` itself, whose source is not here yet, and because PYTHONPATH below is the
# single import mechanism rather than an editable install plus a path; --no-dev keeps pytest
# and pyyaml out of a runtime image.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app/hub
COPY hub/pyproject.toml hub/uv.lock /app/hub/
RUN uv sync --frozen --no-install-project --no-dev

# The wrapper launcher and its rootfs, as ONE layer next to each other. Two things about this
# layout are load-bearing and neither is a preference:
#
#   * `<dir>/wrapper-lite-rootless` + `<dir>/rootfs` is the only arrangement the launcher
#     accepts. It chroots into "./rootfs" *relative to its own working directory*
#     (wrapper-lite-rootless.c:131-138), and WrapperSupervisor reproduces that mapping on the
#     host side to place the 2FA file (`_twofa_file`). A different layout means the hub hands
#     the child a code in a path the child cannot see, and `submit_2fa` is right to refuse.
#   * The rootfs is COPYed rather than bind-mounted, so it is root-owned in the image. The
#     launcher unshare(CLONE_NEWUSER)s with a single-uid map ("0 0 1") before touching the
#     filesystem, which leaves any file owned by another uid unmapped -- a host tree owned by
#     uid 1000 is unwritable to the child even under CAP_DAC_OVERRIDE (spike §5.1 F2). The
#     container must therefore run as root, and compose must not set `user:`.
#
# *** These two COPYs are from the builder stage, not from the build context. *** They used to be
# `COPY wrapper/...` and carried a warning that a fresh clone could not build the image at all,
# because both paths are gitignored upstream build outputs. That warning was true and it was
# also the reason the image was unpinnable, so it was removed rather than reworded: stage 1
# builds both, and the assertions in the RUN below are now about a compiler that failed to
# produce its output rather than about a file that was never in the context.
#
# The `test -x /opt/wrapper/rootfs/system/bin/lite` is the one that matters most. `lite` is the
# Android payload the launcher execs inside the chroot, and the build writes it to
# `rootfs/system/bin/` as a side effect of the `lite` target's RUNTIME_OUTPUT_DIRECTORY. A
# configure that succeeded and a build that produced no payload look identical from here.
COPY --from=wrapper-build /src/wrapper/wrapper-lite-rootless /opt/wrapper/wrapper-lite-rootless
COPY --from=wrapper-build /src/wrapper/rootfs /opt/wrapper/rootfs
RUN set -eu; \
    chmod 0755 /opt/wrapper/wrapper-lite-rootless; \
    # A missing payload surfaces at runtime as `execve: Permission denied` from inside a
    # chroot, which reads like a namespace problem. Assert the layout the launcher needs.
    test -x /opt/wrapper/rootfs/system/bin/lite; \
    test -d /opt/wrapper/rootfs/system/lib64; \
    test -d /opt/wrapper/rootfs/proc; \
    test -d /opt/wrapper/rootfs/dev

# The vendored client, and the config it will read.
#
# The config is the part that needs explaining, because it cannot live anywhere else.
# `ConfigCreator.create` calls `Config.load_from_config()` with its own default argument --
# the literal relative string "config.toml" -- and upstream has no seam and no creart hook to
# override it. So the only file the client will ever open is <CWD>/config.toml, and RipperHost
# holds the CWD at the vendor root for its whole life to make that true. A config mounted at
# any other path is not a different config: it is a config that is silently ignored while the
# image's own copy is used instead.
#
# So it is built here, at <vendor>/config.toml, from upstream's config.example.toml. That
# keeps one source of truth for the ~150 settings instead of forking a copy of them, and it
# keeps the operator's host config.toml -- gitignored, and excluded by .dockerignore -- out of
# the image. Three things are overridden, and each one is a different kind of reason:
#
#   * dirPathFormat / playlistDirPathFormat become ABSOLUTE and point at AMD_DOWNLOAD_ROOT.
#     Relative,
#     they resolve against the vendor root the seam chdirs into, so the client would write to
#     /app/AppleMusicDecrypt/downloads/... while the library scan read the bind mount at
#     /library: it would fill a tree the hub never scans, and every track would be
#     re-downloaded forever, with no error anywhere. And /library is where compose mounts
#     the operator's library, so downloads land in the tree the hub
#     deduplicates against, which is the entire reason for mounting it there at all.
#   * region.language, because upstream's example ships zh-Hant-HK and the operator's own
#     config.toml is ja. Metadata language is a per-account preference rather than a build
#     input, so it is an ARG: set it in compose's `build.args`, or with
#     `docker build --build-arg AMD_VENDOR_LANGUAGE=...`.
#   * nothing else. [localInstance].enable stays false and [instance].url stays on 12340,
#     which are already the right values for this topology, and both are asserted below
#     rather than rewritten -- an image that "fixes" them by sed is a second source of truth.
#
# Every substitution is asserted immediately after it is made. A sed whose pattern stops
# matching -- an upstream rename, a reformatted default -- would otherwise ship a config that
# is wrong in exactly the way that fails silently.
# Re-declared inside the stage on purpose. A global `ARG` (the one above the first FROM,
# which is what compose's `build.args` sets) is NOT in scope in a stage's instructions, so
# without this line `${AMD_DOWNLOAD_ROOT}` would expand to nothing here and every sed below
# would write `/{album_artist}/{album}`. Same reason `AMD_VENDOR_LANGUAGE` is here and not
# only at the top.
ARG AMD_DOWNLOAD_ROOT
ARG AMD_VENDOR_LANGUAGE=ja
COPY AppleMusicDecrypt /app/AppleMusicDecrypt
RUN set -eu; \
    cd /app/AppleMusicDecrypt; \
    cp config.example.toml config.toml; \
    sed -i "s|^dirPathFormat = .*|dirPathFormat = \"${AMD_DOWNLOAD_ROOT}/{album_artist}/{album}\"|" config.toml; \
    sed -i "s|^playlistDirPathFormat = .*|playlistDirPathFormat = \"${AMD_DOWNLOAD_ROOT}/playlists/{playlistName}\"|" config.toml; \
    sed -i "s|^language = .*|language = \"${AMD_VENDOR_LANGUAGE}\"|" config.toml; \
    grep -qx "dirPathFormat = \"${AMD_DOWNLOAD_ROOT}/{album_artist}/{album}\"" config.toml; \
    grep -qx "playlistDirPathFormat = \"${AMD_DOWNLOAD_ROOT}/playlists/{playlistName}\"" config.toml; \
    grep -qx "language = \"${AMD_VENDOR_LANGUAGE}\"" config.toml; \
    grep -qx 'enable = false' config.toml; \
    grep -qx 'url = "127.0.0.1:12340"' config.toml; \
    /opt/venv/bin/python -c "import tomllib; c = tomllib.load(open('config.toml','rb')); \
        assert c['download']['dirPathFormat'] == '${AMD_DOWNLOAD_ROOT}/{album_artist}/{album}'; \
        assert c['download']['playlistDirPathFormat'] == '${AMD_DOWNLOAD_ROOT}/playlists/{playlistName}'; \
        assert c['region']['language'] == '${AMD_VENDOR_LANGUAGE}'; \
        assert c['localInstance']['enable'] is False"

# The hub, last: it is the input that changes most often, so it belongs in the layer that is
# cheapest to rebuild. hub/tests/ and hub/spike/ are excluded by .dockerignore -- a runtime
# image has no pytest and no host-relative wrapper path to probe. hub/deploy/ IS copied, but
# only the one file below, because the gate has to run inside the image it is checking.
COPY hub/hub /app/hub/hub
COPY hub/deploy/build_gate.py /app/build_gate.py

# PYTHONPATH is /app/hub -- the directory that CONTAINS the package.
#
# `import hub` needs a sys.path entry holding `hub/__init__.py`. The package is COPYed to
# /app/hub/hub, so that entry is /app/hub. The plan's Step 1 says /app, which is the *project*
# directory: it holds `hub/`, but that is a plain directory with no `__init__.py`, so it can
# only ever contribute a namespace package whose `hub.app` does not resolve.
#
# **The honest caveat, because the failure mode is easy to overstate: with the WORKDIR below,
# /app works anyway.** Under `-m`, sys.path[0] is the working directory, and WORKDIR is
# /app/hub -- already the package root -- so the CWD supplies the real package before
# PYTHONPATH is consulted. Measured inside this image, `import hub.app`:
#
#     PYTHONPATH   CWD         hub.__path__            result
#     /app/hub     /app/hub    ['/app/hub/hub']        regular   <- shipped
#     /app         /app/hub    ['/app/hub/hub']        regular   <- the plan's; works by accident
#     (unset)      /app/hub    ['/app/hub/hub']        regular   <- also by accident
#     /app/hub     /           ['/app/hub/hub']        regular   <- shipped, CWD moved
#     /app         /           ['/app/hub']            NAMESPACE <- the plan's, CWD moved
#     (unset)      /           import fails outright
#     /app         /app        ['/app/hub', '/app/hub'] NAMESPACE
#
# So /app/hub is the only value that is correct *independently of the working directory* --
# a `working_dir:` in compose, a different base image, or running a script from anywhere else
# turns /app into a namespace package. It fails the build gate first, which is a better place
# to find out than at the first request. Do not "simplify" this back to /app on the grounds
# that it currently works: it works because of WORKDIR, not because of the value.
#
# The vendor tree is deliberately *not* on PYTHONPATH: `src.*` has to keep resolving through
# the seam's own sys.path insert, which is what the boundary test in test_ripper_host.py
# enforces.
ENV PYTHONPATH=/app/hub

# AMD_WRAPPER_BINARY points at the ROOTLESS launcher, and that is not interchangeable with
# the default this client ships:
#   * wrapper-lite-qemu has no host rootfs at all and passes --base-dir *into the guest*, so
#     the 2FA code the hub writes lands in a namespace the hub cannot see. Host-side 2FA
#     cannot work with it, and WrapperSupervisor.submit_2fa refuses by design rather than
#     writing a file nobody will read.
#   * wrapper-lite-qemu also needs KVM, a guest image and a boot, none of which this image
#     has.
# config.py's DEFAULT_WRAPPER_BINARY still names the QEMU launcher and is left alone: it is
# correct for the upstream desktop deployment this client was cloned from, and changing it
# would make the hub's default wrong for the case the default actually describes.
ENV AMD_WRAPPER_BINARY=/opt/wrapper/wrapper-lite-rootless

# Absolute, every one of them, for the same reason the rest of this file's paths are:
# RipperHost holds the process working directory at the vendor root for its whole life, so a
# relative path handed to the hub resolves against /app/AppleMusicDecrypt and fails silently
# on reads.
ENV AMD_WRAPPER_BASE_DIR=/data/wrapper
ENV AMD_DB_PATH=/data/hub.db
# Matches the vendor config's [instance].url, and is deliberately NOT published to the host.
ENV AMD_WRAPPER_HOST=127.0.0.1
ENV AMD_WRAPPER_PORT=12340
ENV AMD_BIND=0.0.0.0
ENV AMD_PORT=8080
# With no AMD_LIBRARY_ROOTS the image would fall back to config.py's DEFAULT_LIBRARY_ROOTS,
# which are host paths that do not exist in the container and would be reported as degraded
# roots forever. compose always sets it; this is the single-root fallback for `docker run`.
ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}

EXPOSE 8080

# `python -m hub.app`, not `uvicorn hub.app:app`. main() reads the environment and serves on
# one worker, so the settings and the lifespan the process actually runs are the ones the
# factory was handed. A `--workers 1` that lives only in a compose file is a default someone
# can raise, and the broker, the job store, the leaf registry, the scheduler and the session
# generation all live on app.state -- so a second worker would be a second of each of those:
# two schedulers claiming jobs, two leaf registries, and a logout that revokes only the
# sessions its own worker minted.
CMD ["/opt/venv/bin/python", "-m", "hub.app"]

# The build gate, last, so it sees the finished image. AMD_PASSWORD is a throwaway for this
# step only -- load_settings will not build Settings without one and the gate has to reach
# vendor_config_path() through the real code path -- and it is never written anywhere.
RUN AMD_PASSWORD=build-time-check-only /opt/venv/bin/python /app/build_gate.py
