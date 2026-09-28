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

# --- Upstream repositories -----------------------------------------------
# Both upstream repos are cloned at build time, pinned by commit hash. This replaces
# the old submodule arrangement: no .gitmodules, no gitlinks, no submodule status.
# The commit hashes are the pin -- update them deliberately, the same way a submodule
# pin was moved.
#
# **Full 40-character hashes, not the 7-character abbreviations these used to carry.** An
# abbreviated SHA is a prefix that resolves only while no other object shares it, so a pin
# written that way is not a pin: upstream can add an object that makes it ambiguous, and
# `git checkout` then refuses. Full hashes are also checkable, which the abbreviations were
# not -- `git rev-parse` in a clone of the same repo either produces this value or it does
# not. `wrapper`'s `rootfs/` is 101 tracked .so files, so a re-pin changes the payload, and a
# pin that is worth arguing about deserves to be unambiguous.
ARG VENDOR_URL=https://github.com/WorldObservationLog/AppleMusicDecrypt
ARG VENDOR_COMMIT=8b609df027facb824f0f16f0fd42c2354b1cfce3
ARG WRAPPER_URL=https://github.com/itouakirai/wrapper
ARG WRAPPER_COMMIT=c61dea9a09627300818a026879565a213be54b73
#
# amd-hub runtime image.
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

# git is not optional: `FetchContent` clones cJSON and Dobby. `unzip` unpacks the NDK, and
# `aria2` is how the NDK itself arrives -- upstream's own Dockerfile fetches it the same way,
# and a segmented download is what makes a 692 MB fetch survivable on a flaky link.
# `ca-certificates` is named because that fetch is https, and "the base image happens to have
# it" is not a fact worth depending on.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates unzip aria2; \
    rm -rf /var/lib/apt/lists/*

# Spelled exactly as upstream's Dockerfile spells it -- `NDK_VERSION=23` there, `r23b` here --
# so two builds of the same wrapper are visibly the same build. Same argument, same URL shape,
# same fetcher.
#
# **This is not a knob to turn, and the name is misleading about that.** Two reasons, both
# inherited from upstream:
#
#   * the `b` suffix is literal, because only some NDK releases are lettered. `NDK_VERSION=24`
#     produces a URL that 404s rather than a different NDK.
#   * the toolchain path above resolves through a directory CMakeLists hardcodes as
#     `./android-ndk-r23b/`. Raise the version and the extracted directory is
#     `android-ndk-r25b/`, cmake finds no compiler, and the stage fails at configure time with
#     a message that never mentions the version.
#
# r23b is the revision the payload and both flags below were recorded against; the
# `-Wall -Werror` argument above is why that is the whole point rather than a detail.
#
# **The download is not verified, and neither is upstream's.** An earlier revision of this file
# carried an `ANDROID_NDK_SHA256` digest and asserted in a comment that it had been computed
# from a fresh download and cross-checked by SHA-1 against the host copy. That could not be
# substantiated, and an unverified claim that reads as a supply-chain guarantee is worse than
# stating the gap: a digest nobody can re-derive is a tripwire that fires for the wrong reason.
# What protects this stage is that the URL is versioned, the artifact is Google's, and the
# payload's behaviour is asserted after the build (`test -x ./rootfs/system/bin/lite`) rather
# than before it.
ARG NDK_VERSION=23

# Re-declared here for the same reason `AMD_DOWNLOAD_ROOT` is re-declared in the runtime
# stage: a global `ARG` is not in scope inside a stage's instructions, only in the `FROM`
# lines. Omitted for four args once, and the image did not build at all -- `sh` said
# `WRAPPER_URL: parameter not set` and the stage failed. Bare, so the global default is
# inherited and `compose`'s `build.args` still overrides it.
ARG WRAPPER_URL
ARG WRAPPER_COMMIT

# Clone the wrapper repo at the pinned commit.
#
# **Full clone, deliberately, and not the shallow-plus-explicit-fetch this used to be.** The
# earlier form was `git clone --no-checkout --depth 1` followed by
# `git fetch --depth 1 origin <sha>`, on the reasoning that a shallow clone lacks the history
# a specific commit lives in. The reasoning is right and the remedy is wrong: that `fetch`
# asks the *server* for a ref **named** `<sha>`, and an abbreviated SHA is not a ref name.
# GitHub does not set `uploadpack.allowAnySHA1InWant`, so the request is answered
# `fatal: couldn't find remote ref` -- and writing the SHA out in full does not help, because
# the problem is the lookup, not its length. The arrangement only worked at all because the
# pin happened to be a branch tip *that had been pushed*; re-pinning to any older commit, on
# any branch, would have broken the build.
#
# A full clone fetches every ref, so the commit is present whatever it is, and a full 40-char
# SHA resolves unambiguously. It costs about 49 MB across the two repositories, against a
# 692 MB NDK this stage has already downloaded, so there was never a reason to economise here.
RUN set -eux; \
    git clone --quiet "$WRAPPER_URL" /src/wrapper; \
    git -C /src/wrapper checkout --quiet "$WRAPPER_COMMIT"

# The stage's working directory, and the absence of this line is why the build has never
# once got past the compile step. Without it the CWD is `/` (debian:bookworm-slim's
# default) and every relative path in the next RUN resolves against the wrong tree:
#
#   - `cmake -S . -B build` saw `/`, and stopped with
#     `CMake Error: The source directory "/" does not appear to contain CMakeLists.txt`.
#   - The NDK's own `aria2c -o ...` / `unzip -q ...` wrote and extracted beside it, putting
#     `android-ndk-r23b/` at `/android-ndk-r23b/` -- one directory away from where it is
#     looked for. `wrapper/CMakeLists.txt:6` says
#     `set(ANDROID_NDK_PATH "${CMAKE_CURRENT_SOURCE_DIR}/android-ndk-r23b")`, and
#     CMAKE_CURRENT_SOURCE_DIR is wherever CMakeLists.txt was found, so the toolchain has
#     to be a *sibling* of the source, not merely somewhere on disk. That failure is
#     quieter than the cmake one: a wrong compiler path is a configure error, or worse a
#     build against a toolchain nobody meant to select.
#
# **Placed here, between the clone and the NDK, and nowhere else works.** After the clone
# because the directory has to exist and already hold the checkout; before the NDK because
# that is what makes the download and its extraction land next to CMakeLists.txt. Putting it
# after the NDK would keep the layer cached and leave the toolchain in the wrong place, and
# putting it at the top of the stage would have the clone write into its own CWD.
#
# Upstream spells this the same way: `WORKDIR /app`, then `unzip -q -d /app`, then
# `COPY ./ ./`, then `cmake -S /app` -- one directory for the toolchain, the source and the
# build, which is what this line recreates. Note what is *not* carried over: upstream also
# installs LLVM through `apt.llvm.org`'s llvm.sh, and this stage deliberately does not.
# CMakeLists pins both compilers to the NDK's own clang
# (`${TOOLCHAIN}/bin/x86_64-linux-android22-clang`), so the host never selects a compiler,
# and upstream's `lsb-release`/`gnupg` exist only to serve llvm.sh.
WORKDIR /src/wrapper

RUN set -eux; \
    aria2c -o "android-ndk-r${NDK_VERSION}b-linux.zip" \
        "https://dl.google.com/android/repository/android-ndk-r${NDK_VERSION}b-linux.zip"; \
    unzip -q "android-ndk-r${NDK_VERSION}b-linux.zip"; \
    rm "android-ndk-r${NDK_VERSION}b-linux.zip"

# Two flags, and either one alone is a build that fails later and more confusingly -- the first
# at configure time, the second in the payload:
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
# Stage 2: the vendored client
# ---------------------------------------------------------------------------
# The second of the two upstream pins, cloned in a stage of its own rather than in the
# runtime one -- which is what `wrapper` already gets, and for the same two reasons.
#
# **The clone used to live in the runtime stage, and died there with
# `/bin/sh: 1: git: not found` (exit 127).** That stage installs
# `ca-certificates curl ffmpeg`; `git` went into `wrapper-build` because *that* stage
# clones, and nothing noticed that the runtime stage does the same job with none. It
# could not have been noticed: every run so far had died in an earlier stage, which is
# how a command nobody has ever executed keeps its mistake.
#
# Cloning here also keeps `git` out of the shipped image. Upstream's own runtime stage
# installs nothing at all (`FROM`/`COPY`/`chmod`/`CMD`), and a package manager in a
# stage that will only ever read a tree is a cost with no use behind it. A re-pin
# invalidates this stage alone as well: installing `git` into the runtime stage moves
# its apt layer and takes the `uv sync` above it down with it -- about 80 s of
# dependency resolution spent because a commit hash changed.
FROM debian:bookworm-slim AS vendor-clone

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Re-declared here for the same reason `wrapper-build` declares its two: a global `ARG`
# is in scope for `FROM` lines only, not for a stage's instructions. Bare, so the global
# default is inherited and compose's `build.args` still overrides it -- and so
# `test_every_arg_a_stage_uses_is_declared_in_that_stage` sees it in this stage.
ARG VENDOR_URL
ARG VENDOR_COMMIT

RUN set -eux; \
    git clone --quiet "$VENDOR_URL" /app/AppleMusicDecrypt; \
    git -C /app/AppleMusicDecrypt checkout --quiet "$VENDOR_COMMIT"

# ---------------------------------------------------------------------------
# Stage 3: the Go hub, compiled
# ---------------------------------------------------------------------------
# **The hub is Go now; the Apple client is still Python, and the boundary is the pipe.**
# `hub-go` is this repository's own code -- settings, the library walk, dedup, the job
# store, the scheduler, the wrapper supervisor and the HTTP/SSE API -- and it is compiled
# here into the one binary the runtime stage starts. The client is deliberately *not*
# ported: it is the part that talks to somebody else's servers through FairPlay and
# Widevine, it is already tuned against them, and a second implementation would drift from
# upstream in ways nobody would notice until a track refused to download. It crosses the
# boundary as a subprocess (`hub-go/tools/pyworker.py`, copied into the runtime stage
# below), one JSON object per line in each direction.
#
# **`golang:bookworm`, not Debian's `golang-1.19` package**, for the same reason the NDK
# above is pinned by version rather than inherited: the module declares `go 1.20`, and an
# older toolchain would make the build fetch a newer one over the network, which is a
# build that fails whenever the network does. The tag is the pin.
FROM golang:1.22-bookworm AS hub-build

# A C compiler, because the job store is SQLite: `internal/sqlite` declares
# `#cgo LDFLAGS: -l:libsqlite3.so.0`, and the runtime stage installs that library
# explicitly (see the runtime deps below). The headers are not needed -- the package
# carries the declarations it uses -- but the file `-l:` names has to exist at link time,
# and `libsqlite3-0` is what provides it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends libsqlite3-0; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src/hub-go

# Only the build inputs. `tools/`, `bench/` and `testdata/` are development material --
# the worker script among them is copied into the runtime stage on its own -- and copying
# the whole directory would put the golden files and the synthetic library in a layer
# nothing reads.
#
# **One COPY per destination, and that is not tidiness.** `COPY a b dest/` copies the
# *contents* of each source directory into dest -- so `COPY hub-go/cmd hub-go/internal
# /src/hub-go/` produced `/src/hub-go/amdhub` and `/src/hub-go/app`, no `cmd/` and no
# `internal/` at all, and `go build ./cmd/amdhub` failed with a pattern that matched no
# packages. The first build of this stage found it; the three lines below are the layout
# the module's import paths describe.
COPY hub-go/go.mod /src/hub-go/go.mod
COPY hub-go/cmd /src/hub-go/cmd
COPY hub-go/internal /src/hub-go/internal

# **`GOPROXY=off` is the point rather than a workaround for a sandbox.** The module has no
# third-party dependencies at all -- standard library plus cgo -- so a build that wants to
# fetch anything is a build that has grown a dependency, and failing loudly here is the
# cheapest place to find out. `-trimpath` keeps `/src/hub-go/...` out of the binary's
# stack traces, and there is no `-ldflags` version string: a version that can disagree with
# the image tag is worse than no version at all.
# `mkdir -p /out` is not decoration. `go build -o` writes its output file but does not
# create the directory above it, so the first real build of this stage -- which is the
# first real build of anything, since the stage is new -- died with `open /out/amdhub: no
# such file or directory` before compiling a line. The runtime stage copies /out/amdhub out
# of here, and that name is the only contract between the two.
RUN set -eux; \
    mkdir -p /out; \
    CGO_ENABLED=1 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
        go build -trimpath -o /out/amdhub ./cmd/amdhub; \
    test -x /out/amdhub

# ---------------------------------------------------------------------------
# Stage 4: the runtime image
# ---------------------------------------------------------------------------
FROM python:3.13-slim

# ca-certificates: the client talks to the Apple API and CDN over TLS. The base image has it,
# and it is named here so that "the image works" cannot depend on that staying true.
# curl: compose's healthcheck target, and the first thing anybody debugs with.
# ffmpeg: Phase 3's post-save ALAC integrity check. Upstream treats a missing ffmpeg as a
# warning rather than an error, so this is a convenience today and a requirement later --
# but it is cheap now and expensive to retrofit, which is the plan's reason for including it.
# uv: build-time only. The venv is at /opt/venv and the worker is started from it directly,
# so nothing re-resolves the lock at container start.
# libsqlite3-0: the Go hub's job store links SQLite dynamically (`hub-go/internal/sqlite`
# declares `#cgo LDFLAGS: -l:libsqlite3.so.0`). The base image carries it for its own
# `sqlite3` module, and it is named here for exactly the reason ca-certificates is: a binary
# that runs because of a coincidence of the base image is a binary that stops running when
# the base image changes.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        curl \
        ffmpeg \
        libsqlite3-0 \
    && pip install --no-cache-dir uv==0.12.9 \
    && rm -rf /var/lib/apt/lists/*

# Ordered by how rarely each input changes, so an edit to the hub does not invalidate the
# 121 MB wrapper layer below it. --frozen refuses to re-resolve, so the image is
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
#     uid 1000 is unwritable to the child even under CAP_DAC_OVERRIDE. The
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
    # chroot, which reads like a namespace problem -- so assert what the image has to ship:
    # the binary the launcher execs, and the libraries it loads.
    #
    # **Deliberately not asserted: `rootfs/proc` and `rootfs/dev`.** This block used to
    # require both, and it passed where it was written while failing in every clone since.
    # `wrapper/.gitignore:91` ignores `rootfs/` as a whole -- the 101 tracked files are
    # `git add -f` exceptions -- and git cannot carry an empty directory at all, so `proc/`
    # (empty) and `dev/` (holding one 0-byte `urandom`) are not in a checkout. What the
    # author's tree did hold was evidence that the launcher had already run there: the
    # forked child creates both itself, before the chroot, as
    # `mkdir("./rootfs/dev", 0755) && errno != EEXIST` (wrapper-lite-rootless.c:103),
    # `open("./rootfs/dev/urandom", O_CREAT | O_RDWR, 0666)` (:108) and
    # `mkdir("./rootfs/proc", 0755) && errno != EEXIST` (:120). None of them assume the
    # path is already there, and a failure is `perror`ed by name rather than surfacing as
    # the `execve` above. Requiring them here asserted the machine that had last run the
    # launcher -- a description of a layout, not the layout this image actually carries.
    test -x /opt/wrapper/rootfs/system/bin/lite; \
    test -d /opt/wrapper/rootfs/system/lib64

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
# only at the top. Four ARGs were missed on the first pass through this file -- the two
# clone args among them -- and the symptom is a build that fails with `parameter not set`,
# after the NDK has already been downloaded.
# `test_every_arg_a_stage_uses_is_declared_in_that_stage` is what stops it recurring.
ARG AMD_DOWNLOAD_ROOT
ARG AMD_VENDOR_LANGUAGE=ja

# The client itself, taken from the stage that clones it. `VENDOR_URL` and `VENDOR_COMMIT`
# are deliberately no longer declared here, because nothing in this stage reads them: they
# are the clone's arguments, and the clone -- with the `git` it needs -- is `vendor-clone`'s
# business. Keeping an ARG beside the instructions that use it is exactly what the test
# named above asks for, and two ARGs declared for a stage that never mentions them would be
# the same error in its quieter form.
COPY --from=vendor-clone /app/AppleMusicDecrypt /app/AppleMusicDecrypt
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
# cheapest to rebuild. hub/tests/ is excluded by .dockerignore -- a runtime
# image has no pytest and no host-relative wrapper path to probe. hub/deploy/ IS copied, but
# only the one file below, because the gate has to run inside the image it is checking.
COPY hub/hub /app/hub/hub
COPY hub/deploy/build_gate.py /app/build_gate.py

# The two halves of the boundary, and both are asserted before the image is finished.
#
# The binary is the hub. It is COPYed from the builder rather than compiled here for the
# usual reason -- the runtime image has no toolchain and must not grow one -- and the
# `test -x` is the same argument as the wrapper payload's check above: a stage that
# produced nothing looks exactly like a stage that produced something until the container
# starts, and then it is `exec: "/usr/local/bin/amdhub": no such file or directory`.
#
# The worker script is the other half. It is `hub-go/tools/pyworker.py` copied to /app
# rather than part of the package, because it belongs to the Go tree: it exists to be the
# Python end of the pipe, it imports `hub.ripper_host` (which *is* the package above), and
# its docstring is the protocol's specification.
COPY --from=hub-build /out/amdhub /usr/local/bin/amdhub
COPY hub-go/tools/pyworker.py /app/pyworker.py
RUN set -eu; \
    test -x /usr/local/bin/amdhub; \
    test -f /app/pyworker.py

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

# --- The Go hub's window onto the client ----------------------------------
# Five variables, and every one of them is absolute for the same reason the paths above
# are: RipperHost holds the process working directory at the vendor root for its whole
# life, so a relative path handed to the hub resolves against /app/AppleMusicDecrypt and
# fails on a read, silently.
#
# AMD_PYTHON is the venv interpreter and not `python3`. The worker imports the client --
# creart, temari, pywidevine -- and those live in /opt/venv; a bare `python3` would start,
# fail the import, and exit, which the hub reports one screen away as "URLs cannot be
# expanded" and "downloads cannot run".
ENV AMD_PYTHON=/opt/venv/bin/python
# The worker script, absolute: it is started as `$AMD_PYTHON $AMD_PYTHON_WORKER --config
# $AMD_VENDOR_CONFIG`.
ENV AMD_PYTHON_WORKER=/app/pyworker.py
# The worker's working directory, and the directory `import hub` resolves through.
#
# **`AMD_WORKER_PYTHONPATH` rather than `PYTHONPATH`, and that is deliberate.** The hub
# hands the worker a *constructed* environment (`config.Settings.WorkerEnv`) instead of
# inheriting its own, so that a stray `PYTHONPATH` in the container -- pointed at some
# other `hub` -- cannot make the worker import a different package than the image
# installed. That failure looks exactly like a working worker until a URL fails to expand.
ENV AMD_WORKER_DIR=/app/hub
ENV AMD_WORKER_PYTHONPATH=/app/hub
# The client's config, named rather than derived. The Python hub derived it from the
# package's own location (`parents[2] / "AppleMusicDecrypt"`), which the build gate above
# still asserts; the Go side is handed the value, and this is the path that gate verified.
ENV AMD_VENDOR_CONFIG=/app/AppleMusicDecrypt/config.toml

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
# There is no default for this one, anywhere. `load_settings` raises if it is unset, so this
# ENV is not a fallback over a bad default -- it is the only thing that lets a bare
# `docker run` of this image start at all. It names the container-side path, which the
# `dirPathFormat` seds above also derive from, so the client's write target and the hub's
# scan target cannot come apart. A `docker run` with no bind mounted at that path starts,
# reports the root as degraded, and scans nothing -- so the bind is not optional in
# practice, only in syntax.
ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}

EXPOSE 8080

# The hub binary, and there is no worker-count flag to get wrong.
#
# `python -m hub.app` used to be the entry point, because a bare `uvicorn hub.app:app` would
# take `--workers` from the command line -- a knob this deployment must not have, since
# everything the hub owns lives in one process: the SSE broker, the job store, the leaf
# registry, the scheduler, the session generation and the wrapper supervisor. Two of those
# would be two schedulers racing `claim_next` (atomic, so no double rip, but two leaf
# registries, so a job could be claimed by a process that never expanded it) and two session
# generations, so a logout on one would not revoke a session minted by the other.
#
# The Go binary takes **no arguments at all** -- everything it reads comes from the
# environment -- so the property holds by construction rather than by a flag being absent
# from a command line. `cmd/amdhub/main.go` carries the argument, and
# `test_one_process_and_the_setting_is_not_reachable_from_compose` reads it there.
CMD ["/usr/local/bin/amdhub"]

# The build gate, last, so it sees the finished image. AMD_PASSWORD is a throwaway for this
# step only -- load_settings will not build Settings without one and the gate has to reach
# vendor_config_path() through the real code path -- and it is never written anywhere.
RUN AMD_PASSWORD=build-time-check-only /opt/venv/bin/python /app/build_gate.py
