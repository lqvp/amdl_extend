"""The application: the singletons, their order, and the factory.

The scheduling concern -- the loop, the pool, the park vocabulary and the progress path
back from the seam -- lives in `hub/scheduler.py` and is wired in by the lifespan below.

**The startup order is not a list, it is a dependency chain.** The wrapper comes up first
because everything downstream of it is a client of it; the ripper host comes second because
`creart` cannot un-register a creator, so a host that failed to start has poisoned the
process for good and there is exactly one chance to start it; the scheduler comes last
because it is the only thing that uses both. `close()` reverses it, and the one ordering
there is not a convention: the scheduler is stopped and **awaited** before the ripper is
closed, because `RipperHost.close()` refuses while a rip is in flight and it is right to --
it holds the process working directory, which upstream resolves the FairPlay template and
the download directory against, and restoring it under a running rip fails silently for
reads.

**A wrapper that will not start does not stop the hub.** A fresh install has no Apple
account, so `supervisor.start()` fails with "no account is logged in", and that is the state
the login page exists for. Booting past it is what makes the hub usable at all; refusing to
boot would make "add your account" impossible. What is not optional is the message: it is
kept verbatim in `state.startup_error` and shown until a start succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tomllib
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from hub.api.jobs import JOBS_CHANNEL, LeafRegistry
from hub.config import Settings, load_settings
from hub.events import EventBroker
from hub.jobs import JobStore, Progress
from hub.ripper_host import RipperHost
from hub.scheduler import Scheduler, run_one, run_pool, scheduler_loop
from hub.state import HubState
from hub.wrapper_supervisor import WrapperSupervisor


def download_root_from_format(value: str) -> Path:
    """Return the static absolute root before the first format field.

    The vendor receives a path template such as ``/library/{album_artist}/{album}``.
    Only the prefix before the first field is the fixed output root. Do not call
    ``resolve()`` here: a symlink is an operator-selected mount path, not permission to
    compare a different physical tree. Parent traversal is rejected even when it appears
    after a format field, because the template must not be able to escape the root.
    """
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("download.dirPathFormat must be an absolute path")
    if ".." in value.split("/"):
        raise ValueError("download.dirPathFormat must not contain '..'")

    prefix = value.split("{", 1)[0].rstrip("/") or "/"
    root = Path(prefix)
    if not root.is_absolute():
        raise ValueError("download.dirPathFormat has no absolute static root")
    # normpath is lexical; unlike Path.resolve it neither follows nor checks symlinks.
    return Path(os.path.normpath(root))


def validate_download_root(config_path: Path, library_roots: Sequence[Path]) -> None:
    """Refuse startup unless the vendor's write root is covered by a scanned root."""
    config_value = "<unavailable>"
    roots_text = ", ".join(str(root) for root in library_roots) or "<none>"
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        config_value = config["download"]["dirPathFormat"]
        write_root = download_root_from_format(config_value)
        contained = any(
            write_root == root or write_root.is_relative_to(root)
            for root in library_roots
        )
        if contained:
            return
        reason = f"write root {write_root} is outside all configured library roots"
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError, ValueError) as exc:
        reason = str(exc)

    raise RuntimeError(
        "unsafe vendor download path: "
        f"[download].dirPathFormat={config_value!r}; "
        f"Settings.library_roots=[{roots_text}]; "
        f"config={config_path}: {reason}"
    )


#: The AppleMusicDecrypt config the seam reads. Derived from this file's own location, never
#: from the working directory, for the reason in `ripper_host`'s own docstring: the hub's CWD
#: is `/app` in the container and `hub/` in a checkout, and both are wrong. And it must be
#: **absolute**, because `RipperHost` holds the process working directory for its whole life
#: and a relative path would resolve against `AppleMusicDecrypt/`.
def vendor_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "AppleMusicDecrypt" / "config.toml"


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #
def create_app(
    settings: Settings | None = None,
    *,
    supervisor: WrapperSupervisor | None = None,
    ripper: RipperHost | None = None,
    autostart: bool = True,
    ripper_config_path: Path | None = None,
) -> FastAPI:
    """Build the application. The one place the singletons are made.

    `settings` defaults to the process environment, so `uvicorn hub.app:app` is not a thing
    that has to be arranged; a caller that has settings in hand (a test, or a second
    deployment sharing the module) passes them.

    **`supervisor` and `ripper` are injection points, not an alternative design.** Both exist
    because the real ones need a wrapper binary and an AppleMusicDecrypt checkout, and the
    behaviour under test -- the wiring, the queue, the dedup decision -- needs neither. What
    is *not* injectable is the resolver: a fake expansion would make `POST /api/jobs` a test
    of the fake, and the six-non-idempotent-keys bug lives exactly on that seam.

    `autostart=False` skips the three startup steps and starts no scheduler, so a caller can
    drive `app.state.run_one()` a step at a time. The lifespan is still entered, so shutdown
    ordering -- which is where `RipperHost.close()`'s in-flight refusal lives -- is real
    under it.

    `ripper_config_path` exists for the same reason as the other two. It is resolved to an
    absolute path here rather than in `RipperHost`, because the host holds the process
    working directory for its whole life and a relative one would resolve against
    `AppleMusicDecrypt/` and fail silently for reads.
    """
    from fastapi import FastAPI as _FastAPI

    from hub import api
    from hub.api.wrapper import wrapper_state  # noqa: F401 - imported for its side effects

    resolved = settings if settings is not None else load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: _FastAPI):
        state = app.state
        # Captured here, not at build time: `create_app` is called before there is a loop in
        # some contexts (a sync test, a REPL), and the progress callback needs the loop that
        # is actually running the scheduler. It is read and closed-checked on every tick.
        state.loop = asyncio.get_running_loop()
        try:
            if autostart:
                # The vendor's write target and the hub's scan roots must overlap before any
                # long-lived process is started. Otherwise downloads can silently accumulate
                # in a tree that deduplication never scans.
                validate_download_root(state.ripper_config_path, state.settings.library_roots)

                # 1. The wrapper. A failure is recorded, not raised: a fresh install has no Apple
                #    account, and the login page is how that gets fixed.
                from hub.wrapper_supervisor import SupervisorError

                try:
                    await state.supervisor.start()
                except SupervisorError as exc:
                    state.startup_error = str(exc)
                    _log(state, f"the wrapper did not start: {exc}")
                else:
                    state.startup_error = None

                # 2. The client. A failure here *is* fatal for the process -- `creart` cannot
                #    un-register a creator, so a half-finished registration turns every later
                #    attempt into a `ValueError` about a duplicate target. The seam's own message
                #    says so; letting it propagate is what makes the container restart.
                await state.ripper.start()

                # 3. The scheduler, last: it is the only thing that needs both.
                state.scheduler = asyncio.create_task(Scheduler(state).run())

            yield
        finally:
            if state.scheduler is not None:
                await Scheduler(state).drain()
            # The host second and only once, and only if it was ever started. `close()`
            # refuses while a rip is in flight, so `_drain` has already waited; and it
            # refuses a *second* start in the same process, so a re-entered lifespan has to
            # leave it alone rather than try again.
            if getattr(state.ripper, "started", False):
                with contextlib.suppress(Exception):
                    await state.ripper.close()
            with contextlib.suppress(Exception):
                await state.supervisor.stop()
            with contextlib.suppress(Exception):
                state.jobs.close()

    # `openapi_url=None` and not just `docs_url`/`redoc_url`. The latter two disable the
    # *browsers* that render the schema; the schema itself is a separate route, mounted by
    # default at `/openapi.json`, and it was answering 200 with the whole API surface to
    # anyone on the LAN. It is a `Route`, not an `APIRoute`, so the session guard never saw
    # it and no route-table test could have found it by the other means either.
    #
    # A machine-readable inventory of every route, every parameter and every response shape
    # is exactly what an attacker reads before choosing one, and the hub has no use for its
    # own -- nothing here generates a client, and the one consumer is a browser. So it is off,
    # and `test_the_only_unauthenticated_route_is_health` holds it off by reading
    # `app.openapi_url` rather than by trusting this line.
    app = FastAPI(
        title="amd-hub",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # -- the singletons ----------------------------------------------------
    # The bag of attributes that used to hang off `app.state` one at a time is now a
    # declaration: `hub.state.HubState`. Same fields, same rules, `create_app` still
    # the only constructor -- what changed is that the interface sits in one file and
    # `slots=True` keeps it from growing an undeclared attribute. The invariant
    # comments live on the fields now; what stays here is the wiring.
    from hub.auth import SessionStore

    # The host is built here rather than in the lifespan, because `on_progress` has to
    # be handed over at construction: the seam is one-per-process and `start()` may
    # never be retried, so an attribute set later would arrive too late for the rip it
    # is meant to describe.
    #
    # **`_progress`, not `partial(_on_progress, state)`.** The round-1 code had the
    # `partial`, which does not bind the state -- it builds a callable that, when the
    # seam calls it with a `Progress`, invokes `_on_progress(state, progress)`, and
    # `_on_progress` takes *one* argument and *returns* the callback. So every progress
    # tick raised `TypeError` inside the seam's sampler, where it became an unretrieved
    # task exception, and the progress column stayed empty for the whole download.
    # `test_create_app_hands_the_seam_a_live_progress_callback` calls what the factory
    # handed over, so it cannot come back.
    def _progress(reading: Progress) -> None:
        # Late binding: `state` does not exist while its own constructor arguments are
        # evaluated, and the callback fires long after them, so the closed-over object
        # is resolved at call time -- exactly what `_on_progress(app.state)` did.
        # The scheduler owns the progress path now: `Scheduler.forward_progress` holds
        # the `current_job` check and the `call_soon_threadsafe` hop to the loop. The
        # Scheduler is stateless beyond holding `state`, so late-binding it here is the
        # same late binding the closure has always had.
        Scheduler(state).forward_progress(reading)

    config_path = (
        Path(ripper_config_path).resolve() if ripper_config_path else vendor_config_path()
    )
    state = HubState(
        settings=resolved,
        sessions=SessionStore(secret=resolved.session_secret),
        broker=EventBroker(),
        jobs=JobStore(resolved.db_path),
        leaves=LeafRegistry(),
        ripper_config_path=config_path,
        # An injected `ripper` is left exactly as given: a test's fake already has
        # whatever behaviour it is asserting, and wrapping it in a real host to attach
        # a callback would defeat the injection.
        ripper=(
            ripper
            if ripper is not None
            else RipperHost(config_path, on_progress=_progress)
        ),
        supervisor=(
            supervisor
            if supervisor is not None
            else WrapperSupervisor(
                binary=resolved.wrapper_binary,
                base_dir=resolved.wrapper_base_dir,
                host=resolved.wrapper_host,
                port=resolved.wrapper_port,
                log_sink=lambda line: _log(state, line),
            )
        ),
        templates=api.build_templates(),
    )
    # The driving surface is bound after construction because each partial names
    # `state` itself; a constructor cannot hand an object to itself.
    state.jobs_counts = partial(_jobs_counts, state)
    state.run_one = partial(run_one, state)
    state.run_pool = partial(run_pool, state)
    state.scheduler_loop = partial(scheduler_loop, state)
    app.state = state

    api.install(app)
    return app


def _jobs_counts(state: HubState) -> dict[str, int]:
    """How many jobs are in each state. One table read, no cache.

    `total` is the sum rather than a separate `COUNT(*)`, so it cannot disagree with the
    parts if a status is ever added to the store's vocabulary without being added here.
    """
    return state.jobs.counts()


def _log(state: HubState, line: str) -> None:
    """The wrapper's own output, onto the stream and onto stderr.

    **The supervisor scrubs this before it gets here** (R6: credentials and tokens are
    replaced before the line leaves the child's pipe), which is why the hub can display a
    child's log lines at all. It is published on the jobs channel so one WebSocket carries
    the queue and the log together, in the order they happened.
    """
    state.broker.publish(JOBS_CHANNEL, {"kind": "log", "line": line})
    import sys

    print(line, file=sys.stderr, flush=True)


def main() -> None:
    """`python -m hub.app` -- read the environment and serve.

    One function, so that the deployment's entry point is this repository rather than a
    `uvicorn` invocation naming a factory: the lifespan and the settings have to be the same
    objects either way, and a `uvicorn hub.app:create_app --factory` line in a compose file
    is a place for them to stop being.

    **Single worker, and it is not a default that can be overridden by accident.**
    Everything the app owns lives on `app.state`, and `app.state` is the declared
    `hub.state.HubState`: the broker, the job store, the leaf registry, the scheduler
    and the session generation. Two workers would be two brokers (so a WebSocket
    subscriber would see only its own worker's events), two schedulers racing `claim_next`
    (which is atomic, so no double rip -- but two leaf registries, so a job could be claimed
    by a worker that never expanded it), and two session generations, so a logout on one
    would not revoke a session minted by the other.

    Long-lived connections and in-process state are the reason this is not a "just turn up
    the workers" situation, and it is worth stating here rather than leaving it as a comment
    in a compose file nobody reads.
    """
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.bind,
        port=settings.port,
        log_level="info",
        workers=1,
    )


if __name__ == "__main__":  # pragma: no cover - the module's entry point
    main()
