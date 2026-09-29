#!/usr/bin/env python3
"""The image's build-time gate -- run by the Dockerfile's last `RUN`, not by the tests.

**Why this is a script and not a `RUN python -c`.** It has to be a file for one reason and
one only: a here-document inside a `RUN` depends on the build frontend's heredoc support,
and the failure mode when a frontend does not have it is a parse error that reads like a
typo. As a committed script it is also reviewable on its own, runnable by hand against any
tree, and assertable on by `tests/test_deployment.py`.

**What it is for.** "The image built" is the least interesting claim this task can make, and
there are two ways to build an image that is broken in a way a build log will never show:

* the vendor root derivation moved. `ripper_host._VENDOR_ROOT` and `app.vendor_config_path`
  are both `parents[2] / "AppleMusicDecrypt"`, so the client has to sit at a fixed depth
  below the package. Move one tree and the seam's error message names a path that exists
  and is not the one it wanted -- at boot, per restart, after the build already succeeded.
* `config.toml` is not in the image. `ConfigCreator` calls `load_from_config()` with its own
  default, so the client reads `<CWD>/config.toml` and there is no seam to point it
  elsewhere. A missing file at <vendor>/config.toml is a `RipperHostError` at boot, and a
  *wrong* file somewhere else is worse: it is silently not used.

So the gate asserts the layout and the config through the real code paths
(`hub.app.vendor_config_path`, `hub.config.load_settings`) rather than by re-deriving them
here. A second derivation in this file would be a third place to update and would test
itself.

`AMD_PASSWORD` is read from the environment and must be set by the caller. The Dockerfile
passes a throwaway; `load_settings` has no other way to be reached, and no credential is
written anywhere by this script. It is a *build* check, so it deliberately does not check
anything that only becomes knowable at runtime: whether the wrapper starts, whether an
Apple account is logged in, whether the library roots are mounted.

**The imports are at module scope, on purpose, and the failure that buys is acceptable.** A
wrong `PYTHONPATH` therefore dies with a bare `ModuleNotFoundError: No module named 'hub'`
rather than a `BUILD GATE FAILED:` line. That is worth it: the gate has to be able to *be* the
thing that reports a layout problem, which it cannot do if reaching its own code is the first
thing that breaks. The build still fails, which is the part that is load-bearing, and the
message names the missing module and the path search. Making the imports lazy would only move
the same information behind two more lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

import hub.app
from hub import ripper_host
from hub.config import load_settings

# Where the package is installed, and therefore where `parents[2]` has to point. Both
# constants are asserted against reality below rather than assumed: the point of the gate is
# that a layout change fails here rather than at the first container start.
VENDOR_ROOT = Path("/app/AppleMusicDecrypt")
WRAPPER_DIR = Path("/opt/wrapper")


def fail(message: str) -> None:
    print(f"BUILD GATE FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    settings = load_settings()

    # -- the wrapper launcher and its rootfs --------------------------------
    # The layout is a contract with `wrapper-lite-rootless.c`, not a convention: it chroots
    # into "./rootfs" relative to its own working directory, and WrapperSupervisor
    # reproduces that same mapping on the host side to place the 2FA file. A launcher
    # without a sibling rootfs/ is a launcher the hub cannot do 2FA with.
    if settings.wrapper_binary.parent != WRAPPER_DIR:
        fail(
            f"AMD_WRAPPER_BINARY is {settings.wrapper_binary}, which is not in "
            f"{WRAPPER_DIR}. The rootfs has to be a sibling of the launcher."
        )
    if not settings.wrapper_binary.is_file():
        fail(f"{settings.wrapper_binary} is not in the image")
    if not (settings.wrapper_binary.parent / "rootfs").is_dir():
        fail(
            f"{settings.wrapper_binary.parent / 'rootfs'} is not in the image. The "
            f"launcher chroots into ./rootfs relative to its own directory, so there is "
            f"nowhere for the 2FA code to land."
        )
    if "-qemu" in settings.wrapper_binary.name:
        fail(
            f"AMD_WRAPPER_BINARY is {settings.wrapper_binary.name}, the QEMU launcher. It "
            f"passes --base-dir *into the guest* and has no host rootfs, so host-side 2FA "
            f"cannot work with it at all, and this image has no guest to boot."
        )
    payload = settings.wrapper_binary.parent / "rootfs" / "system" / "bin" / "lite"
    if not payload.is_file():
        fail(f"{payload} is missing; the launcher would fail at execve()")

    # -- the vendor derivation ---------------------------------------------
    # The base both derivations hang off is `parents[2]` of the installed package. This is
    # compared against the code's own answers rather than used to recompute them, so that a
    # change in either function is caught here instead of becoming a silent mismatch at
    # boot: recomputing would make this file a third derivation that agrees with itself.
    derived_base = Path(hub.app.__file__).resolve().parents[2]
    if derived_base != VENDOR_ROOT.parent:
        fail(
            f"the package is at {Path(hub.app.__file__).resolve()}, so parents[2] is "
            f"{derived_base} and the client would have to be at {derived_base}/"
            f"AppleMusicDecrypt, but the image put it at {VENDOR_ROOT}. "
            f"ripper_host._VENDOR_ROOT and app.vendor_config_path are both "
            f"parents[2] / 'AppleMusicDecrypt', so one of the two trees has to move."
        )
    # And then the two answers that actually matter, asked of the two functions that make
    # them, so a disagreement between the seam and the app is a build failure too.
    if ripper_host._require_vendor_root() != VENDOR_ROOT:
        fail(
            f"the seam resolves the vendor root to {ripper_host._require_vendor_root()}, not "
            f"{VENDOR_ROOT}"
        )
    config = hub.app.vendor_config_path()
    if config != VENDOR_ROOT / "config.toml":
        fail(f"vendor_config_path() is {config}, not {VENDOR_ROOT / 'config.toml'}")
    if not config.is_file():
        fail(
            f"{config} is not in the image. AppleMusicDecrypt's Config.load_from_config() "
            f"opens the relative path 'config.toml' with no way to override it, so this is "
            f"the only file it will ever read."
        )

    # -- the paths the image passes ----------------------------------------
    # Every one of these is absolute, and that is not tidiness: RipperHost holds the process
    # working directory at the vendor root for its whole life, so a relative path handed to
    # the hub resolves against /app/AppleMusicDecrypt and fails silently on reads.
    for name in ("wrapper_base_dir", "db_path"):
        value = getattr(settings, name)
        if not value.is_absolute():
            fail(f"settings.{name} is {value}, which is not absolute")
    for root in settings.library_roots:
        if not root.is_absolute():
            fail(f"library root {root} is not absolute")
    # There is no "AMD_LIBRARY_ROOTS resolved to nothing" check here any more, and its
    # absence is not an oversight: `load_settings` now refuses to build a Settings with an
    # empty `library_roots`, so this gate can never observe one. An empty scan is stopped at
    # startup by the caller, named in the message it raises, rather than here.

    # The client's wrapper endpoint and the supervisor's must be the same loopback address,
    # or the client talks to a port nothing is on. `[instance].url` is "host:port" with no
    # scheme, so it is compared as text.
    import tomllib

    with config.open("rb") as handle:
        upstream_config = tomllib.load(handle)
    expected = f"{settings.wrapper_host}:{settings.wrapper_port}"
    actual = upstream_config["instance"]["url"]
    if actual != expected:
        fail(
            f"{config}'s [instance].url is {actual!r} but the hub supervises the wrapper on "
            f"{expected!r}. The client would talk to a port nothing is listening on."
        )
    if upstream_config["localInstance"]["enable"]:
        fail(
            "[localInstance].enable is true, so the client would launch its own QEMU "
            "backend and overwrite [instance].url. The hub supervises the wrapper; "
            "the client must not."
        )
    for key in ("dirPathFormat", "playlistDirPathFormat"):
        value = upstream_config["download"][key]
        if not value.startswith("/"):
            fail(
                f"[download].{key} is {value!r}, which is relative. The seam chdirs into "
                f"the vendor root, so a relative download path writes to "
                f"/app/AppleMusicDecrypt/downloads/... while the library scan reads the "
                f"bind mount -- the client would fill a tree the hub never scans and every "
                f"track would be re-downloaded forever."
            )
    if not upstream_config["download"]["dirPathFormat"].startswith(
        settings.library_roots[0].as_posix()
    ):
        # This compares against the *image's* root, AMD_DOWNLOAD_ROOT, which is the right
        # comparison at build time and is NOT the operator's runtime configuration.
        # `AMD_LIBRARY_ROOTS` can be edited in `.env`; the application lifespan's
        # `validate_download_root` check rejects a mismatch before any service starts.
        fail(
            f"[download].dirPathFormat writes to "
            f"{upstream_config['download']['dirPathFormat'].split('/')[1]!r} but the first "
            f"library root is {settings.library_roots[0]}. Downloads would land outside "
            f"every tree the hub deduplicates against."
        )
    # Runtime root ordering is immaterial: the lifespan compares the static write root
    # against every configured library root, not only the first one.

    # -- the bind address ---------------------------------------------------
    # Not 0.0.0.0 for the wrapper, ever: it is an unauthenticated HTTP API that serves
    # decrypted audio, and publishing it would be a data leak, not a feature. This asserts
    # the loopback default rather than trusting compose to have spelled it.
    if settings.wrapper_host not in ("127.0.0.1", "::1", "localhost"):
        fail(
            f"AMD_WRAPPER_HOST is {settings.wrapper_host}. The wrapper's API is "
            f"unauthenticated and must never be reachable off the container's loopback."
        )

    print(
        f"BUILD GATE OK  vendor_root={VENDOR_ROOT} config={config} "
        f"binary={settings.wrapper_binary} library_roots={settings.library_roots}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
