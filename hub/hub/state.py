"""The application's declared internal interface: `HubState`.

Before this file existed the interface lived as attributes dynamically attached to
`app.state` -- twenty-three of them, documented as prose across six modules, with a
typo surfacing as a runtime `AttributeError` on a rare path. The fields are the same,
the rules are the same, and `create_app` is still the only constructor; what changed is
that the interface is now *declared*, in one file, `slots=True` so the seam cannot grow
an undeclared attribute at all ("a walk that inspects nothing must not report success",
the same discipline `tests/test_ripper_host.py` applies to imports).

The write set is deliberately narrow and every writer is named where the field lives:
`create_app` constructs, the lifespan fills the two lifespan fields, `hub.app` owns the
five scheduler-owned fields, and `hub.api` owns the six API-visible ones. Nothing else
writes. `hub/tests` reaches the app only through `create_app`, so the radius of an
interface change is this file plus the one constructor.

The field docstrings carry the invariants that used to be prose beside the assignments
in `hub/app.py`; the *why* stories stay where the behaviour lives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.templating import Jinja2Templates

from hub.api.jobs import LeafRegistry
from hub.auth import SessionStore
from hub.config import Settings
from hub.events import EventBroker
from hub.jobs import JobStore
from hub.ripper_host import RipperHost
from hub.wrapper_supervisor import LoginChallenge, WrapperSupervisor


@dataclass(slots=True)
class HubState:
    """Everything the app owns, declared. Constructed only by `hub.app.create_app`."""

    # -- the wired singletons (constructed by create_app, never reassigned) ----
    settings: Settings
    """The resolved environment. Every path in it is absolute, because `RipperHost`
    holds the process working directory for its whole life."""

    sessions: SessionStore
    """The signed-cookie verifier. No server-side table; `generation` retires tokens."""

    broker: EventBroker
    """The in-process fan-out behind `GET /api/jobs/stream`. Not thread-safe, and it
    does not need to be: one event loop, one process."""

    jobs: JobStore
    """The queue. One SQLite connection, autocommit, `claim_next` atomic."""

    leaves: LeafRegistry
    """`job id -> Leaf` for the expansions this process made; a miss means a
    re-expansion, and a failed re-expansion fails the job naming the id."""

    ripper_config_path: Path
    """`<vendor root>/config.toml`, resolved absolute before `RipperHost` chdirs."""

    ripper: RipperHost
    """The seam. Built behind the injection point so a test's fake keeps its own
    behaviour; the real one carries the progress callback from construction."""

    supervisor: WrapperSupervisor
    """The launcher's owner. Its `status()` is uncached on purpose -- readiness is a
    fact to observe, never to hold."""

    templates: Jinja2Templates
    """One Jinja environment per app; a fresh one per request would re-compile."""

    # -- the scheduler driving surface (bound by create_app right after build) --
    # Partials over `state` itself, so the binding is the one legal moment after
    # construction. Tests drive `run_one()`/`run_pool()` through these; the
    # signatures are the interface.
    jobs_counts: Callable[[], dict] | None = None
    """Queue shape per status, one table read, no cache."""

    run_one: Callable[[], Awaitable[bool]] | None = None
    """Claim and run at most one job -- the step a test drives without a race."""

    run_pool: Callable[[], Awaitable[int]] | None = None
    """`rip_concurrency` workers until nothing claimable is left; how many ran."""

    scheduler_loop: Callable[[], Awaitable[None]] | None = None
    """The loop body, exposed so `autostart=False` callers can start it themselves."""

    # -- lifespan-owned ----------------------------------------------------------
    loop: asyncio.AbstractEventLoop | None = None
    """The running loop, captured when there is one; the progress callback hops to it."""

    scheduler: asyncio.Task | None = None
    """The loop task when `autostart`. `_drain` awaits it before any close."""

    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    """Set on shutdown; every sleep wakes on it so `docker stop` is not a stall."""

    # -- operational state (named owners, listed in the module docstring) --------
    startup_error: str | None = None
    """The supervisor's verbatim start failure, kept until a start succeeds; the
    dashboard answers "not running" with it. Written by `hub.app` and `hub.api.wrapper`."""

    pending_2fa: LoginChallenge | None = None
    """The login child's outstanding challenge; `submit_2fa` names it from here.
    Written by `hub.api.wrapper` only."""

    current_job: int | None = None
    """The job the scheduler is running, non-`None` only for the duration of a rip;
    `None` between jobs is what makes a late progress reading harmless."""

    cached_problem: str | None = None
    """The last readiness answer, and it is *never read as an answer* -- every claim
    follows a fresh probe. Exists so status callers skip a round-trip."""

    degraded_roots: tuple[str, ...] = ()
    """The last unreadable-root set, for once-per-change warnings only."""

    ripping_adam_ids: dict[str, int] = field(default_factory=dict)
    """The one in-flight table per process: adam_id -> job id. `adam_id`-only because
    that is the key upstream's task table answers on; two codecs, one rip."""

    session_generation: int = 0
    """Logout bumps it; bumping retires every token ever issued. Written by
    `hub.api` on logout only."""
