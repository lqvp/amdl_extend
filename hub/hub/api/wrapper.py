"""The wrapper's lifecycle, its Apple login, and the hub's own status.

Three rules in this file are load-bearing, and each of them exists because the alternative
produces a state the user cannot tell from a broken one.

**A successful login is followed by a restart.** The payload reads its token cache at process
start (`lite_main.cpp:614`), so a wrapper that was already serving when the account was
logged into keeps serving with the *old* -- or no -- account. Reporting success on the login
call alone therefore leaves a serving-but-unauthenticated wrapper: `regions` stays empty, every
download returns nothing, and the page says "logged in". So `_finish_login` restarts before
it answers, and only reports success once a restarted wrapper actually reports regions.

**`regions: []` is not ready, and it is not the same as "not running".** The supervisor says
so in two different sentences that ask two different things of the user -- "log in" and
"wait / look at the launcher's output" -- and the hub passes both through verbatim. What it
adds is `problem`, a machine-readable key with two values, so a template can pick the right
button without matching on English prose. `test_status_reports_the_two_unready_states_
differently` holds the distinction; `test_a_start_failure_keeps_the_supervisors_own_wording`
holds the wording.

**The 2FA window is 60 seconds, not five minutes.** `auth.cpp:83-88` polls `20 x sleep(3)`
and then exits, so a deadline longer than that offers the user a code nobody will read. The
deadline is applied here as well as in the supervisor because the supervisor's own
`twofa_ttl` is configurable and this is the value the *response* promises.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

from hub.api import fail, guarded
from hub.wrapper_supervisor import SupervisorError, observe_readiness

router = guarded()

#: The payload's own poll loop, `20 x 3s`. Not the supervisor's configurable
#: `twofa_ttl` -- this is what the hub tells the user it will wait, and it must not promise
#: more than the child honours.
TWOFA_DEADLINE = 60.0

# The supervisor's two readiness messages, quoted. `classify_supervisor_failure` is a
# fallback, not the primary path: wherever the state can be *observed* it is, and these are
# only read for the state that cannot be re-probed. See that function for why.
NO_ACCOUNT_MARKER = "no account is logged in on the wrapper"
NOT_READY_MARKER = "did not become ready"

#: What the hub says when it can observe the state itself: the wrapper is up, `/status`
#: answers, and `regions` is empty. Written so that the two states cannot be confused by a
#: reader -- it says "nothing needs to be waited for", because that is the part the
#: supervisor's own wording is careful about and the part a user acts on.
NO_ACCOUNT_DETAIL = (
    "no account is logged in on the wrapper: it is up and answering /status, but regions is "
    "empty, so it cannot serve a download. Log in from this page; the wrapper itself is "
    "ready, so there is nothing to wait for."
)

NOT_RUNNING_DETAIL = (
    "no wrapper is running. Start it from this page; if it was started and stopped, the "
    "reason is in the log below."
)


def classify_supervisor_failure(message: str) -> str:
    """`"no-account"` or `"unavailable"`, from the supervisor's own wording.

    **A live probe is better and is used wherever it can be.** `_wrapper_state` asks the
    wrapper what is actually true, and that is the answer this function only approximates
    here. The case it exists for is a `start()` that *failed*: `_wait_ready` tears the child
    down before it raises, so by the time the hub sees the error there is nothing left to
    ask, and the message is the only evidence left in existence.

    Matching on the collaborator's prose is a real dependence and worth naming: if upstream
    rewrites either sentence, this returns `"unavailable"` for a no-account state -- which
    shows the *correct* message (the verbatim one is always what reaches the user) with a
    coarser key beside it. So the failure is "the button is the generic one", not "the user
    is told to wait". `test_a_start_failure_keeps_the_supervisors_own_wording` holds the
    wording itself, which is the half that matters.
    """
    if NO_ACCOUNT_MARKER in message:
        return "no-account"
    return "unavailable"


async def wrapper_state(state) -> dict:
    """What the wrapper is doing, as the UI needs it.

    Every field is a fact about the wrapper rather than about this hub's opinion of it:
    `running` is the supervisor's own, `regions` is the payload's, and `detail` is either a
    message this module wrote (for the state it observed) or the supervisor's, unchanged.
    """
    supervisor = state.supervisor
    running = bool(supervisor.running)
    regions: list[str] = []
    problem: str | None = None
    detail: str | None = None

    if running:
        readiness = await observe_readiness(supervisor)
        regions = list(readiness.regions)
        if readiness.kind == "no-account":
            # The distinct state: serving, healthy, and unable to serve a download
            # because no account is on it. `NOT_READY_MARKER` is deliberately absent
            # from `detail` so that the two messages cannot be read as one.
            problem = "no-account"
            detail = NO_ACCOUNT_DETAIL
        elif readiness.kind == "unreachable":
            # "Cannot be asked", and the probe's own error is all that may honestly
            # be said about the wrapper.
            problem = "unavailable"
            detail = readiness.detail
    else:
        # A `start()` that failed is the one case where nothing can be probed, and the
        # message it left behind is the only evidence there is. It is classified rather than
        # collapsed, because "the wrapper is not running because nobody has logged in" and
        # "the wrapper is not running because it did not start" need different buttons --
        # and the first is the state every fresh install is in.
        detail = state.startup_error or NOT_RUNNING_DETAIL
        problem = (
            classify_supervisor_failure(detail) if state.startup_error else "unavailable"
        )

    return {
        "running": running,
        "adopted": bool(supervisor.adopted),
        "pid": supervisor.pid,
        "port": supervisor.bound_port,
        "regions": regions,
        "ready": problem is None,
        "problem": problem,
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def _start(state) -> Response:
    """Start the wrapper, or report the supervisor's own reason for not being able to.

    `state.startup_error` is kept so that a later `/api/status` can still explain a wrapper
    that is not running: without it, a failed start becomes "not running" with no reason
    the moment the request returns, which is the state the user has to debug from.
    """
    try:
        await state.supervisor.start()
    except SupervisorError as exc:
        state.startup_error = str(exc)
        return fail(502, str(exc), problem=classify_supervisor_failure(str(exc)))
    state.startup_error = None
    return await wrapper_state(state)


@router.post("/api/wrapper/start")
async def start(request: Request) -> Response:
    return await _start(request.app.state)


@router.post("/api/wrapper/stop")
async def stop(request: Request) -> dict:
    state = request.app.state
    await state.supervisor.stop()
    state.startup_error = None
    return await wrapper_state(state)


@router.post("/api/wrapper/restart")
async def restart(request: Request) -> Response:
    state = request.app.state
    await state.supervisor.stop()
    return await _start(state)


# --------------------------------------------------------------------------- #
# Apple login
# --------------------------------------------------------------------------- #
class _LoginBody(BaseModel):
    """Every field optional, so a missing one is a wrong password rather than a 422.

    A 422 would say "field required" where a 401 says "that did not work", and the second is
    both true and the only answer this route is allowed to give.
    """

    username: str = ""
    password: str = ""


class _TwoFaBody(BaseModel):
    challenge_id: str = ""
    code: str = ""


@router.post("/api/wrapper/login")
async def login(request: Request, body: _LoginBody | None = None) -> Response:
    """Start an Apple login, and answer with either a challenge or a finished restart.

    Two shapes of success, because upstream has two. `WrapperSupervisor.login` runs the
    launcher's login mode and returns once the child *asks for a 2FA code*; an account that
    needs none never gets that far, and the supervisor reports it as an error whose own text
    says "if this account needs no 2FA the login is done". So a raised `SupervisorError`
    here is not necessarily a failure, and the only honest way to tell is to restart the
    wrapper and look at `regions` -- which is what `_finish_login` does, for both paths.
    """
    state = request.app.state
    payload = body or _LoginBody()

    try:
        challenge = await state.supervisor.login(payload.username, payload.password)
    except SupervisorError as exc:
        # Kept, not raised: if the account needed no 2FA this is a success the supervisor
        # has no return value for, and the restart decides.
        return await _finish_login(state, challenge=None, login_error=exc)

    state.pending_2fa = challenge
    return {
        "challenge_id": challenge.id,
        "expires_at": challenge.expires_at,
        "expires_in": max(0.0, round(challenge.expires_at - time.time())),
    }


@router.post("/api/wrapper/login/2fa")
async def submit_2fa(request: Request, body: _TwoFaBody | None = None) -> Response:
    """Hand the code to the waiting child, then restart and only then report.

    The challenge id comes from this process's own memory of the outstanding login rather
    than from the request. The supervisor holds at most one login child -- a second login
    supersedes the first and discards its challenges -- so there is exactly one challenge
    that can be live, and accepting a caller-supplied id would be accepting a second way of
    naming it for no gain. The wire shape's `{code}` is therefore enough.
    """
    state = request.app.state
    challenge = state.pending_2fa
    payload = body or _TwoFaBody()
    if challenge is None:
        return fail(
            400,
            "there is no 2FA code waiting for one. Start the login again: either the "
            "previous attempt was superseded, or that account needs no code at all.",
            problem="no-challenge",
        )
    if not payload.code.strip():
        return fail(400, "the 2FA code is empty, so there is nothing to hand the wrapper.")

    try:
        # Bounded, because the child is not: `auth.cpp` polls for 60 s and then `exit(1)`s.
        # A hung `submit_2fa` past that point would leave the hub holding a challenge the
        # wrapper will never read, and the user retyping a code that cannot arrive.
        await asyncio.wait_for(
            state.supervisor.submit_2fa(challenge.id, payload.code.strip()),
            timeout=TWOFA_DEADLINE,
        )
    except TimeoutError:
        state.pending_2fa = None
        return fail(
            400,
            f"the wrapper was still not reading the code after {TWOFA_DEADLINE:.0f}s, which "
            f"is the window it polls for (`20 x 3s`) before giving up. Log in again to get "
            f"a fresh code.",
            problem="expired",
        )
    except SupervisorError as exc:
        state.pending_2fa = None
        return fail(400, str(exc), problem="rejected")

    state.pending_2fa = None
    return await _finish_login(state, challenge=None, login_error=None)


async def _finish_login(state, *, challenge, login_error) -> Response:
    """Restart the wrapper, resume what was waiting, and report what is true.

    The order is the whole point of this function. `stop()` then `start()`, because the
    payload reads its token cache at start-up; then `resume_waiting()`, because a token
    expiry parks
    jobs in `waiting` rather than failing them and they should run as soon as there is an
    account; and only then an answer, so that "logged in" always means a wrapper that can
    serve a download.

    A failed restart is a failure of the *login*, not a footnote to it: the account is on
    disk and the wrapper that would use it is not up, and the user has to know that.
    """
    supervisor = state.supervisor
    try:
        await supervisor.stop()
        await supervisor.start()
    except SupervisorError as exc:
        state.startup_error = str(exc)
        return fail(
            502,
            str(exc),
            problem=classify_supervisor_failure(str(exc)),
            login_error=None if login_error is None else str(login_error),
        )

    state.startup_error = None
    resumed = state.jobs.resume_waiting()
    current = await wrapper_state(state)
    if current["problem"] is not None:
        return fail(
            502,
            current["detail"],
            problem=current["problem"],
            login_error=None if login_error is None else str(login_error),
        )
    return {"ok": True, "wrapper": current, "resumed": resumed}


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@router.get("/api/status")
async def status(request: Request) -> dict:
    """The wrapper, the library's reachability, and the queue's shape.

    Three sources, none of them cached, and the reason is a deliberate rule: the library
    on disk is the only source of truth, so a drive that was unplugged a second ago has
    to show up here rather than in a cache's opinion of a minute ago.
    """
    from hub.api import _library_summary

    state = request.app.state
    return {
        "wrapper": await wrapper_state(state),
        "library": await _library_summary(state),
        "queue": state.jobs_counts(),
        "queue_paused": state.queue_paused,
        # The scheduler's live shape: who is mid-rip right now against the configured
        # ceiling. Uncached for the same reason as the library -- `2/4 ripping` read from
        # a cache is the number that misleads while a drive is actually moving.
        "pool": {
            "ripping": len(state.ripping_adam_ids),
            "limit": max(1, state.settings.rip_concurrency),
        },
        "dedup_artist_scope": state.settings.dedup_artist_scope,
    }
