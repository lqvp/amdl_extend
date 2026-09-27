"""The application: the singletons, their order, and the download scheduler.

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

**The scheduler runs one job at a time.** `claim_next` is atomic so several workers *could*
run, but there is one `RipperHost`, one process working directory and one wrapper token
budget, and `MVRipper`/`Ripper` both resolve their singletons through it. Sequential
throughput is not the constraint here -- a download takes minutes and the queue is the
user's, not the hub's.

**The filesystem duplicate check happens here and nowhere else.** It is a per-*file*
decision, and a queued job can sit long enough for the file to be deleted underneath it, so
checking at enqueue time would put two dedup checks with different timings into the codebase
for them to disagree. The one input that is easy to get wrong is `track_title`: it is the
**rendered output file name**, from `RipperHost.render_song_filename`, and never
`Leaf.title`, because `normalize` is not idempotent and six of the real library's 8,721 keys
are not fixed points.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from hub.api.jobs import JOBS_CHANNEL, LeafRegistry, _publish_job, job_to_dict
from hub.config import Settings, load_settings
from hub.dedup import DuplicateHit, find_duplicate
from hub.events import EventBroker
from hub.jobs import (
    IllegalTransition,
    Job,
    JobNotFound,
    JobStore,
    Leaf,
    Progress,
)
from hub.ripper_host import RipperHost, RipperHostError
from hub.wrapper_supervisor import WrapperSupervisor

#: How long the loop waits between finds of an empty queue. Short enough that a job enqueued
#: and cancelled in the same breath is noticed promptly, long enough that an idle hub is not
#: waking 2,000 times a second to query an empty table.
IDLE_POLL_SECONDS = 0.5

#: How long to wait before re-probing the wrapper while something is queued but the wrapper
#: cannot serve it.
#:
#: The requirement is that a token expiring while the hub runs is a state change a cache
#: would hide, and that is real -- but 0.5 s was the wrong instrument for it. The loop called
#: `supervisor.status()` (uncached, on purpose) on *every* iteration, including the ones where
#: there was nothing to claim, so an idle hub made 2.0 probes/s: ~172,800 HTTP requests a day
#: to learn repeatedly that a queue nobody is using is empty.
#:
#: The requirement is about *responsiveness to a change*, and responsiveness comes from **when
#: we act**, not from how often we look. So there are now two rates, and an idle hub uses
#: neither:
#:
#: - **Empty queue** -- no HTTP at all. `_has_actionable` is a SQLite read, and readiness
#:   cannot matter when there is nothing to be ready *for*. This is where the 172,800 went.
#: - **Queued but unready** -- this interval, because a user who has just logged in wants
#:   their queue to start and a supervisor restart takes seconds.
#: - **Queued and ready** -- the claim is made immediately after a fresh probe, so there is no
#:   window in which a stale "ready" is acted on (`test_a_claim_is_never_made_on_a_stale_
#:   readiness_answer`).
IDLE_READINESS_POLL_SECONDS = 1.0

#: How long to wait after a job *ran*, before the loop goes round again. Longer than the
#: empty-queue case because a job just running means the queue is draining, and the next
#: thing worth doing is usually nothing at all.
POST_JOB_POLL_SECONDS = 0.5

#: How long a shutdown waits for a job in progress before cancelling it. A download takes
#: minutes, so this is generous; it exists so that a `docker stop` (ten seconds by default)
#: is *not* what decides whether a half-written file is left behind. Cancelling is safe for
#: the host: `RipperHost`'s in-flight counter is released in a `finally`, so `close()` after a
#: cancellation is accepted rather than refused.
DRAIN_TIMEOUT_SECONDS = 300.0

#: The AppleMusicDecrypt config the seam reads. Derived from this file's own location, never
#: from the working directory, for the reason in `ripper_host`'s own docstring: the hub's CWD
#: is `/app` in the container and `hub/` in a checkout, and both are wrong. And it must be
#: **absolute**, because `RipperHost` holds the process working directory for its whole life
#: and a relative path would resolve against `AppleMusicDecrypt/`.
def vendor_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "AppleMusicDecrypt" / "config.toml"


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
async def _worker(state, running, declined, budget) -> int:
    """Claim and run jobs until there is nothing left to claim, or `budget` is spent.

    One worker, looping -- rather than a pass that claims N and waits for the slowest -- so a
    slot that frees is refilled immediately. That is the difference the music-video case turns
    on: a five-minute video in a 4-slot *pass* holds three other slots idle for five minutes,
    which is three tracks not started that could each have been finished in ten seconds.
    Upstream has always had this shape; `safely_create_task` per song, `DownloadManager`'s
    semaphore holding the count. What is new is the number being `AMD_RIP_CONCURRENCY` rather
    than `maxRunningTasks` (128, tuned for a TUI driven by a person).

    `budget` counts jobs actually *run*, not rows looked at, so a pass over a queue it had to
    defer does not spend its budget. `budget=1` is `run_one`.
    """
    ran = 0
    while ran < budget:
        # `exclude`, not "claim it and put it back". Releasing a row makes it the lowest
        # eligible id again, so the next claim hands it straight back, and both this call and
        # the release that preceded it are synchronous -- a worker that kept re-claiming
        # would never reach an `await`, and the event loop would stop entirely. That is not a
        # slow queue, it is a hub that answers no request, streams no progress and ignores
        # `docker stop` until it is killed. Excluding hands back the *next* eligible row, or
        # `None`, which is an answer a loop can act on.
        job = state.jobs.claim_next(exclude=declined)
        if job is None:
            return ran
        if job.id in declined:
            # Unreachable while `exclude` works, and kept anyway because the alternative is
            # the worst failure mode in this module rather than a slow queue: a row that is
            # already declined is `queued` again, so it is the lowest eligible id, so the
            # next claim returns it, and the loop is synchronous throughout -- no `await`, so
            # no task switch, so the whole event loop stops. Nothing above would notice; the
            # hub would simply stop answering. One line to make that unreachable twice over.
            state.jobs.mark(job.id, "queued")
            return ran
        if job.adam_id and job.adam_id in running:
            # Upstream's own guard, one layer up: `rip.py` short-circuits on `adam_id` alone,
            # so the same track in two codecs has to be ripped one after the other. See
            # `run_pool` for why that was unreachable before and what it looks like now.
            declined.add(job.id)
            state.jobs.mark(job.id, "queued")
            continue
        if job.adam_id:
            running[job.adam_id] = job.id
        ran += 1
        try:
            await _execute(state, job)
        except asyncio.CancelledError:
            _mark(state, job, "cancelled", error="the hub shut down while this was running")
            raise
        except Exception as exc:  # noqa: BLE001 - one job's failure is not the loop's
            _mark(state, job, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if job.adam_id:
                running.pop(job.adam_id, None)
            state.leaves.forget(job.id)
    return ran


async def run_pool(state) -> int:
    """Run the queue through `rip_concurrency` workers. How many jobs it ran.

    **Why more than one.** Measured on this machine, not estimated: a 41.8 MB ALAC track
    takes 9.8 s, and 6.1 s of that is the wrapper answering `/lyrics`, the album lookup and
    the codec check before a byte of audio moves. The audio then crosses at ~41 MB/s in about
    a second. So 85% of a track's wall clock is an API round-trip, and a serial queue spent
    almost all of its time waiting for a sibling that was not running.

    **The collision this makes reachable, and why the scheduler owns the guard.** The queue's
    unique index is `(adam_id, codec)`, so the same track in `alac` and in `aac` is two legal
    active jobs. But `rip.py:166` short-circuits on `download_manager.get_task(url.id)` --
    `adam_id` alone, with no codec -- so whichever of the two starts second finds the first
    in the table, returns immediately, and the hub marks it `done` with nothing downloaded.
    Serially that could not happen, because the first unregistered before the second began.

    So a job whose `adam_id` is already in flight is not run: it is released and the claim
    excludes it for the rest of the pass. The rule is upstream's, applied one layer up where
    the whole key is visible. `test_the_same_track_in_two_codecs_is_not_ripped_twice_at_once`
    is the regression test.

    **Collect before re-raising, and say so rather than swallow it.** Plain `gather`
    propagates the first child exception to its awaiter and leaves the siblings *running as
    orphaned tasks* -- it does not cancel them. So the pool collects, lets every worker
    settle, and only then re-raises. What it re-raises is not only `CancelledError`: a worker
    can also die in its claim loop, which the per-job guard does not cover, and four workers
    writing to one SQLite file is four times the chance of `database is locked`. A pool that
    collected and then ignored that would report a drain it did not perform.
    """
    running: dict[str, int] = getattr(state, "ripping_adam_ids", None) or {}
    state.ripping_adam_ids = running
    declined: set[int] = set()
    limit = max(1, state.settings.rip_concurrency)

    results = await asyncio.gather(
        *(_worker(state, running, declined, sys.maxsize) for _ in range(limit)),
        return_exceptions=True,
    )
    # Every worker has settled by now, so re-raising strands nothing. `CancelledError` comes
    # first because a shutdown that is reported as a store error would be a lie about why the
    # container is stopping.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return sum(results)


async def run_one(state) -> bool:
    """Claim and run at most one job. `True` if it did.

    Split out of the loop so that a caller -- a test, or an operator's one-shot -- can drive
    exactly one step without a background task racing it, and so that "claim, decide, run,
    mark" is a single readable unit rather than something interleaved with a sleep.

    A single `_worker` with a budget of one, so there is one implementation of "claim, run,
    mark" and not two. Note what a ceiling of 1 does and does not mean: it is a *sequential
    queue*, not "one job per call" -- the worker keeps claiming until the queue is empty, and
    only the `budget` bounds how much it may run at once. A pool that stopped after one job
    would be the barrier `run_pool` replaced, with none of the benefit.
    """
    return (await _worker(state, _in_flight_table(state), set(), 1)) > 0


def _in_flight_table(state) -> dict[str, int]:
    running: dict[str, int] = getattr(state, "ripping_adam_ids", None) or {}
    state.ripping_adam_ids = running
    return running

async def _execute(state, job: Job) -> None:
    """One job: find its leaf, decide whether it is on disk, and rip it or skip it."""
    leaf = await _leaf_for(state, job)
    if leaf is None:
        _mark(
            state,
            job,
            "failed",
            error=(
                f"adam_id={job.adam_id} could not be expanded from {job.parent_url} any more, "
                f"so there is no album name, artist or storefront to rip it with. The `job` "
                f"table does not store them, so the only way to know them is to ask the "
                f"catalogue -- and it no longer lists this track under that URL. Re-submit "
                f"the URL to queue it again."
            ),
        )
        return

    if not job.force and not leaf.is_music_video:
        # A deliberate non-goal: a music video lives in one flat `mv.saveDir`, so there is no
        # album scope to compare against and it is always re-downloaded. Checking it would
        # either never match or match the wrong thing, and the user must not be told a video
        # was "already downloaded".
        hit = await _filesystem_duplicate(state, leaf)
        if hit is not None:
            _mark(state, job, "skipped", skip_reason=_skip_reason(hit))
            return

    runner = state.ripper.run_music_video if leaf.is_music_video else state.ripper.run_song
    # Set for the duration of the rip and cleared in the `finally` (B3). It used to be set and
    # left set, which meant the *next* job's first reading -- and any stray reading from a
    # sampler that outlived its rip -- was written to a job id that had nothing to do with it.
    # The `None` between jobs is the check that makes those readings harmless rather than
    # merely unlikely.
    state.current_job = job.id
    try:
        await runner(leaf, force=job.force)
    except Exception as exc:  # noqa: BLE001 - one job's failure is not the loop's
        # A wrapper that stops serving *during* a rip parks the job in `waiting` rather
        # than failing it. The discriminator is the wrapper's own observable state, never the
        # message -- see `_park_reason`.
        #
        # One `except`, not two, and the *type* is the first test rather than a separate
        # clause: a `RipperHostError` is the only failure that came from the client and so the
        # only one a wrapper that cannot serve could explain. Anything else -- a
        # `ResolveError` from a re-expansion, an `OSError` from a vanishing library root, a
        # bug -- is not a token question, and parking one of those in `waiting` would retry it
        # for ever on a schedule while never once showing a failure, because `waiting` is not
        # terminal. The same bug as a swallowed exception with a nicer message.
        reason, detail = (
            await _park_reason(state) if isinstance(exc, RipperHostError) else (None, "")
        )
        if reason is not None:
            _park_for(state, job, exc, reason, detail)
        else:
            _mark(state, job, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    finally:
        # Cleared here and not in `run_one`: `_execute` is what owns the id, so a caller that
        # drives it directly gets the same invariant without knowing about `run_one`. The
        # `return` in the `except` above still runs this, so the id is cleared on every path
        # including the parked and failed ones -- which is the whole point of the check.
        state.current_job = None
    _mark(state, job, "done")


def _park_for(state, job: Job, exc: Exception, reason: str, detail: str = "") -> None:
    """Put the job in `waiting`, with a message that is true of *this* reason.

    The rule "走行中ジョブは fail せず `waiting` へ入れ" -- a job interrupted by the wrapper going
    away goes back in the queue rather than becoming a failure, because nothing is wrong with
    the track and the user is at most one action away from a working download. `resume_waiting`
    puts it back at its original place (the queue is ordered by `id` and nothing else), so a
    20-track album interrupted on track 14 does not start over.

    **The reason decides the message**, because the ways out are different and a message that
    names the wrong one is a confidently wrong instruction: a crash is recovered by the
    supervisor or by `POST /api/wrapper/start`, an account that signed out needs a human, and
    an unanswerable health check needs neither. See `PARK_MESSAGES` and `_park_reason`.

    `detail` is the probe's own error, and only `"unreachable"` has a placeholder for it. It
    defaults to `""` rather than being required so that the other two callers do not have to
    invent one, and so that a new reason which forgets the placeholder fails loudly here --
    a `KeyError` in a format string -- instead of quietly rendering a message with a hole in
    it. An unknown *reason* is left to `KeyError` for the same reason: a fourth park reason
    that silently got the crash message would be a worse bug than a 500 on one job.

    `error` is kept, and that is `JobStore`'s contract rather than this module's: the reason
    the queue stopped stays on the row, and `claim_next` clears it when the job actually runs
    again. So the row says both "this is why it paused" and "it has not run since".
    """
    _mark(
        state,
        job,
        "waiting",
        error=PARK_MESSAGES[reason].format(exc=exc, detail=detail),
    )


async def _park_reason(state) -> tuple[str | None, str]:
    """Why the wrapper cannot serve a download right now, and what the probe said about it.

    Returns `(reason, detail)`. `reason` is `None` when a download could run, which is the only
    thing `_execute` branches on; `detail` is the probe's own error text when the reason came
    from a *failure*, and `""` otherwise, so the two-element shape does not vary by case.

    **The discriminator is the wrapper's observable state, not the error's prose.** Matching
    on a substring of the message is exactly the kind of test that dies quietly: upstream
    rewords a log line and the queue starts failing jobs that should have waited, with no
    failing test. So the question asked is `regions`, which the supervisor already reports and
    which means the same thing in every version -- an empty list cannot serve a download, and
    the whole `waiting` mechanism is built on that observation.

    **The return is a reason and not a bool, because the reasons need different messages and
    different ways out (B1b).** A wrapper that has crashed is recovered by the supervisor or by
    `POST /api/wrapper/start`; an account that has signed out needs the user to log in. Telling
    someone "the Apple token expired -- log in from the queue page" about a crash they did not
    cause and cannot fix by logging in sends them to do a pointless thing, and leaves a row
    that reads as a credential problem on a machine with a perfectly good credential.

    **Three reasons, not two, and the third is `"unreachable"` (round 3).** Round 2 lumped a
    *failed probe* in with *a wrapper that is known to be down*, and the two deserve different
    words. A timeout or a connection error on `/status` is not evidence that the wrapper
    stopped: the process may be perfectly healthy and the check may simply have failed to get
    an answer. The evidence standard is the same one that keeps a failed probe from claiming
    the *account* signed out -- an uninformative observation does not support a specific claim
    -- and it applies symmetrically. `supervisor.running` is the observable that answers this
    question honestly: it is the supervisor's own record of whether it has a process.

    **`detail` exists for that case alone.** When the reason is `"unreachable"` the probe's
    error *is* the diagnosis -- it is the only thing known about the wrapper -- so the message
    quotes it alongside the rip's own failure rather than describing a cause it cannot
    support.

    **`"unreachable"` is not a new `wrapper_state.problem` value.** The `/api/status` contract
    keeps its `None` / `"no-account"` / `"unavailable"` set; this function's vocabulary is its
    own, because a park reason is a statement about *why a job was paused* and a status problem
    is a statement about *what a user sees on the dashboard*. They overlap on purpose -- there
    is one answer to "why is the wrapper not ready" -- but they are not the same question, and
    widening the JSON contract for this would be a bigger change than the problem needs.

    A failed probe is still `not ready` for parking purposes: unknown is not ready, and
    `waiting` is re-checked before the next claim, so the safe direction is to wait.
    """
    if not state.supervisor.running:
        return "unavailable", ""
    try:
        regions = (await state.supervisor.status()).get("regions") or []
    except Exception as exc:  # noqa: BLE001 - unknown is not ready, and the safe direction
        return "unreachable", f"{type(exc).__name__}: {exc}"
    return (None if regions else "no-account"), ""


#: The park reasons, and what the user is told to do -- keyed by reason so a message cannot be
#: attached to the wrong cause, which is the failure mode an `if` chain here would have.
#:
#: **Each message states only what was observed.** `"unavailable"` is only reached when
#: `supervisor.running` is `False`, which is the supervisor's own record of having no process,
#: so "the wrapper stopped serving" is a reading of evidence rather than an inference. That is
#: why `"unreachable"` is a separate key rather than a softer version of the same sentence: a
#: failed probe and a known-down wrapper look identical to a reader otherwise, and the first is
#: a claim the hub cannot support. The `{detail}` placeholder holds the *probe's* error and is
#: the only thing known about the wrapper in that case.
#:
#: `{exc}` is always the rip's own failure, which is the diagnosis and is never replaced; a
#: row that is about a failed download should show why the download failed even when the
# reason it was paused is something else entirely.
PARK_MESSAGES = {
    "no-account": (
        "the Apple account signed out while this was downloading: {exc} Log in from the queue "
        "page and it resumes from here."
    ),
    "unavailable": (
        "the wrapper stopped serving while this was downloading: {exc} This resumes on its own "
        "as soon as the wrapper is back -- nothing to log in to, and no action needed unless "
        "the wrapper does not come back."
    ),
    "unreachable": (
        "the check that says whether the wrapper can serve failed while this was downloading, "
        "so it is not known what the wrapper was doing: {detail} The download itself ended "
        "with {exc} The wrapper may well be fine; this resumes on its own as soon as the "
        "check succeeds again."
    ),
}


def _on_progress(state) -> Callable[[Progress], None]:
    """The seam's `on_progress`, wired to a `mark` and a publish.

    Called from a worker thread, so it hops to the loop with `call_soon_threadsafe` rather
    than awaiting -- a callback cannot be async, and touching the SQLite connection from a
    worker would be a second writer besides. One `mark` per callback would be a write per
    chunk; this coalesces, because the interesting question is "how far along is it" and
    `ProgressCallback` in the seam already throttles to a tenth of a second.
    """

    def report(progress: Progress) -> None:
        job_id = state.current_job
        if job_id is None:
            return
        loop = state.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(_apply_progress, state, job_id, progress)

    return report


def _apply_progress(state, job_id: int, progress: Progress) -> None:
    """Write one progress reading and publish it. Runs on the loop.

    **Two ways this can be refused, and both are expected rather than exceptional (B2).** The
    row may be gone -- deleted or cancelled between the chunk and here, with the transfer
    still running upstream and its progress simply no longer wanted -- or the row may have
    *finished*, which is the race that matters: the seam cancels its sampler in a `finally`,
    which stops new readings but not one already handed to the loop with
    `call_soon_threadsafe`, so a callback can arrive after `_mark(..., "done")`.

    Without the second check, that callback set the row back to `running` and cleared
    `finished_at`, at which point `delete` and `retry` both answered 409 ("not finished, so
    there is nothing to retry") and nothing would ever release it: a finished job stuck
    displaying as running, with no user action able to clear it. `JobStore.mark` refuses the
    transition now, and this is the half that turns the refusal into a discarded reading.
    """
    try:
        state.jobs.mark(
            job_id,
            "running",
            progress=progress.fraction,
            bytes_done=progress.bytes_done,
            bytes_total=progress.bytes_total,
        )
    except JobNotFound:
        return
    except IllegalTransition:
        # The job it was describing has finished. The reading is about a transfer that is
        # over, so dropping it loses nothing -- and the row it would have overwritten is the
        # one carrying the real outcome.
        return
    _publish_job(state, job_id)


async def _leaf_for(state, job: Job) -> Leaf | None:
    """The `Leaf` for `job`, or `None` if it cannot be described any more.

    Two sources, in order. The registry holds the expansion this process made, which is the
    common case and free. The second is a re-expansion of the parent URL, which is what makes
    a restart survivable: a queued row outlives the process that wrote it, and the `job` table
    does not carry the fields the rip needs, so the only way to get them back is to ask the
    catalogue again.

    Matching is on `(adam_id, codec)` and not on `adam_id` alone, because a playlist that
    lists a track twice, or an artist whose catalogue holds the same song in two codecs, is
    an ordinary shape and a job must be ripped in the codec it was queued for.
    """
    leaf = state.leaves.get(job.id)
    if leaf is not None:
        return leaf

    web_api = state.ripper.web_api
    if web_api is None:
        return None
    from hub.resolver import ResolveError, expand

    try:
        leaves = await expand(
            job.parent_url, codec=job.codec, language=job.language, web_api=web_api
        )
    except (ResolveError, RuntimeError):
        # A `ResolveError` is "that URL is not expandable any more", which is the answer the
        # caller needs. A `RuntimeError` from the catalogue is a transport problem, and
        # treating it as "gone" would fail a perfectly good track; both land on the same
        # message, which says the expansion failed rather than claiming the track does not
        # exist.
        return None

    for candidate in leaves:
        if candidate.adam_id == job.adam_id and candidate.codec == job.codec:
            state.leaves.put(job.id, candidate)
            return candidate
    return None


async def _filesystem_duplicate(state, leaf: Leaf) -> DuplicateHit | None:
    """The filesystem duplicate check on a real walk, in a worker thread.

    The rendered file name is produced on the loop and the walk happens off it: `os.walk`
    over a 341 GB drive blocks for long enough to stall every other request and the SSE
    stream, and the rendered name needs `creart`, whose cache is not thread-safe.
    """
    rendered = state.ripper.render_song_filename(leaf)
    return await asyncio.to_thread(_find_duplicate, state, leaf, rendered)


def _find_duplicate(state, leaf: Leaf, rendered: str) -> DuplicateHit | None:
    """The pure part of the duplicate check, given its three inputs and a scan.

    `rendered` is the *file name* `rip_song` would write, and it is passed through
    `normalize` exactly once -- on the library side too, since `scan_roots` normalises each
    file it finds. Feeding it `leaf.title` instead would double-normalize, and because
    `normalize` is not idempotent that mis-keys the six real library entries whose rendered
    form is `1-01 1 a.m. (feat. …).m4a`: the file keys to `1 a.m. (feat. …)` while the tag
    title keys to `a.m. (feat. …)`. The result is a re-download of a file that is already
    there, with no error anywhere -- which is why
    `test_the_dedup_check_is_given_the_rendered_file_name_not_the_tag_title` exists.
    """
    from hub.library_scan import scan_roots

    scan = scan_roots(state.settings.library_roots)
    _warn_degraded(state, scan)
    return find_duplicate(
        scan,
        album_name=leaf.album_name,
        track_title=rendered,
        artist_name=leaf.artist_name,
        artist_scope=state.settings.dedup_artist_scope,
    )


def _skip_reason(hit: DuplicateHit) -> str:
    """`duplicate:<path>|<path>`, in the order `find_duplicate` sorted.

    The paths are the whole of the evidence and they reach `skip_reason` intact. `loose`
    matching is deliberately willing to treat two same-named albums as one -- that is what
    catches a collab fanned out across every credited artist's folder, and it is 164 of the
    223 real duplicate album names -- so a false positive is possible and the only way a
    user can overrule it is by reading which directories were matched. The queue page renders
    them verbatim.

    **`hit.resolved`, not `hit.matched`, and the difference is the whole point.** `matched`
    holds bare relpaths and `find_duplicate` pools candidates across every configured root,
    so with two roots those strings name directories that exist under *neither* -- the user
    is told where a track already is and cannot go and look. `resolved` carries the same
    evidence as `roots[root_index] / relpath`, which opens, and which stays distinct when one
    relpath appears under two roots (the 種別 A shape, which is a duplicate album name filed
    in two libraries, not twice in one). The string is longer and less pretty, and that is
    the trade: an unresolvable path is not evidence.
    """
    return f"duplicate:{'|'.join(hit.resolved)}"


def _warn_degraded(state, scan) -> None:
    """Say so, once per change, when a configured root cannot be read.

    An unmounted external drive must be **loud**. `loose` dedup against the surviving
    roots still works, so the failure mode without this is a quiet re-download of
    everything that lived on the missing drive -- and the operator has no way to tell that
    from the hub being broken.
    """
    current = tuple(str(root) for root in scan.degraded)
    if current == state.degraded_roots:
        return
    state.degraded_roots = current
    if current:
        state.broker.publish(
            JOBS_CHANNEL,
            {
                "kind": "library",
                "degraded_roots": list(current),
                "detail": (
                    f"these library roots could not be read: {', '.join(current)}. Downloads "
                    f"continue and duplicates are still detected against the roots that are "
                    f"there, but a track that lived on a missing drive will be downloaded "
                    f"again. Check that the drive is mounted."
                ),
            },
        )
    else:
        state.broker.publish(
            JOBS_CHANNEL, {"kind": "library", "degraded_roots": [], "detail": ""}
        )


def _mark(state, job: Job, status: str, **fields) -> None:
    """Write the terminal (or intermediate) status and tell every open tab.

    `store.get` afterwards rather than reusing the local `job`, because `mark` computes
    `finished_at` and the store is the only place that knows what it set. Publishing a row
    that disagrees with the table is how a queue ends up showing a download that finished
    seconds ago as still running.
    """

    state.jobs.mark(job.id, status, **fields)  # type: ignore[arg-type]
    current = state.jobs.get(job.id)
    if current is not None:
        state.broker.publish(JOBS_CHANNEL, {"kind": "job", "job": job_to_dict(current)})


async def scheduler_loop(state) -> None:
    """Claim, run, sleep; until the shutdown event is set.

    **The readiness probe happens immediately before every claim, not on a timer.** That is
    what replaced the 2 Hz poll: the question "is the wrapper ready right now?" is only
    interesting immediately before acting on the answer, and asking it any other time is a
    request whose only consumer is a sleep. So the loop is

        probe -> claim -> run -> sleep(IDLE_POLL_SECONDS)

    and the probe is one uncached `supervisor.status()` per iteration *that reaches the
    claim*, which for an idle hub is one per `IDLE_READINESS_POLL_SECONDS` rather than two
    per second. `test_an_idle_hub_probes_the_wrapper_rarely_and_one_more_when_waking` counts
    both halves: the idle rate, and that a claim is never made on a stale answer.

    The two states are then both quiet in the right way: a wrapper that is not running
    leaves the queue `queued` (so the user's order is intact and `POST /api/wrapper/start`
    makes it run), and a wrapper serving with no account says so once instead of failing
    every job with an error about `/key`.
    """
    announced: str | None = None
    while not state.stopping.is_set():
        # A SQLite read, not an HTTP request, and that distinction is the whole fix. An idle
        # hub -- no `queued` and no `waiting` job -- is the only state in which readiness
        # cannot matter, so it asks the database and not the wrapper.
        if not _has_actionable(state):
            await _sleep_or_stop(state, IDLE_POLL_SECONDS)
            continue

        # Something is queued, so readiness is about to be acted on and is probed now. A
        # A cached answer here is the bug: a token that expired an hour ago would
        # still read as ready.
        problem = await _wrapper_problem(state)
        state.cached_problem = problem
        if problem is not None:
            if problem != announced:
                announced = problem
                state.broker.publish(
                    JOBS_CHANNEL, {"kind": "wrapper", "problem": problem}
                )
            # The wrapper is not serving. Poll at the *readiness* rate rather than the queue
            # rate, because the thing being waited on is the wrapper coming back and there is
            # nothing to gain from looking for new work twice a second.
            await _sleep_or_stop(state, IDLE_READINESS_POLL_SECONDS)
            continue

        # **The wrapper can serve, so every parked job goes back on the queue** (B1a).
        #
        # This used to happen only on the Apple login path, which is where the round-0
        # producer lived -- and that made a *wrapper crash* a job with no exit: the row said
        # "the token expired, log in", the account was fine, `POST /api/wrapper/start` did
        # nothing for it, `POST /api/jobs/{id}/retry` refused `waiting` with a 409, and
        # neither the scheduler nor anything else would ever touch it. A `waiting` job that
        # no transition can release is the defect; the login path was just the one caller
        # that happened to exist.
        #
        # **Unconditional on every ready probe, not keyed on a transition.** A transition
        # guard sounds tighter and is not: a token that dies and recovers between two probes
        # is never observed unready, a hub that restarts holding parked jobs sees only ready
        # answers, and a crash the supervisor fixes inside `IDLE_READINESS_POLL_SECONDS` is
        # the same case. All three leave `announced is None` with a job nobody will ever
        # release. The cost of calling it anyway is one indexed `UPDATE ... WHERE status =
        # 'waiting'` that matches zero rows, and correctness here is worth one statement per
        # second.
        #
        # The login path also calls `resume_waiting()` itself, for the case where a user logs
        # in and the loop is not running (`autostart=False`, a one-shot script). Whichever
        # gets there first does the work; the other finds nothing and returns 0.
        resumed = state.jobs.resume_waiting()
        if resumed:
            _log(state, f"the wrapper is serving again; {resumed} parked job(s) requeued")
        announced = None

        # A job ran, so the wrapper was ready moments ago; a change since then is more
        # likely to be the kind worth noticing.
        await _sleep_or_stop(
            state,
            POST_JOB_POLL_SECONDS if await run_pool(state) else IDLE_POLL_SECONDS,
        )


def _has_actionable(state) -> bool:
    """Whether the loop has anything to do at all: a `queued` or a `waiting` job.

    Two statuses, and **`waiting` is the one that was missing (B1a).** The check exists to
    avoid an HTTP round-trip to a process in a QEMU guest on an idle hub, and it was written
    as "is anything `queued`". But a queue whose only contents are parked jobs still needs
    the loop running: the loop is the only thing that probes readiness, and readiness is what
    releases them. With `queued` alone, a hub that came up with three parked jobs and a
    healthy wrapper would spin at `IDLE_POLL_SECONDS` asking SQLite, never probe, and never
    resume -- the exact no-exit bug the recovery above fixes, reintroduced through the gate
    that avoids the HTTP call.

    `list(status=...)` is `SELECT * FROM job WHERE status = ? ORDER BY id` over the whole
    table, so this is two such reads rather than free. `any(...)` short-circuits, so the cost
    is the query and not the iteration. It is still two orders of magnitude cheaper than the
    request it avoids, and `test_an_idle_hub_probes_the_wrapper_rarely_and_one_more_when_`
    waking measures the difference.
    """
    return any(True for _ in state.jobs.list(status="queued")) or any(
        True for _ in state.jobs.list(status="waiting")
    )


async def _wrapper_problem(state) -> str | None:
    """`None` when a download could run, else `"no-account"` or `"unavailable"`.

    The supervisor's own `status()`, not the cached `state` this module keeps, because the
    whole point is that a *change* -- the token expiring while the hub runs -- is a state
    change a cache would hide. `WrapperSupervisor.status` is uncached for exactly this reason.
    """
    from hub.api.wrapper import wrapper_state

    if not state.supervisor.running:
        return "unavailable"
    current = await wrapper_state(state)
    return current["problem"]


async def _sleep_or_stop(state, seconds: float) -> None:
    """Sleep, but wake immediately on shutdown so a `docker stop` is not a 0.5 s stall."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.stopping.wait(), timeout=seconds)


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
        if autostart:
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
            state.scheduler = asyncio.create_task(scheduler_loop(state))

        try:
            yield
        finally:
            if state.scheduler is not None:
                await _drain(state)
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
    app.state.settings = resolved
    from hub.auth import SessionStore

    app.state.sessions = SessionStore(secret=resolved.session_secret)
    app.state.broker = EventBroker()
    app.state.jobs = JobStore(resolved.db_path)
    app.state.leaves = LeafRegistry()
    app.state.ripper_config_path = (
        Path(ripper_config_path).resolve() if ripper_config_path else vendor_config_path()
    )
    # The host is built here rather than in the lifespan, because `on_progress` has to be
    # handed over at construction: the seam is one-per-process and `start()` may never be
    # retried, so an attribute set later would arrive too late for the rip it is meant to
    # describe. It is built *after* `state` exists for the same reason -- the callback closes
    # over `app.state` and the loop it hops to is not known until the lifespan runs.
    #
    # **`_on_progress(app.state)`, not `partial(_on_progress, app.state)`.** The round-1 code
    # had the `partial`, which does not bind the state -- it builds a callable that, when the
    # seam calls it with a `Progress`, invokes `_on_progress(state, progress)`, and
    # `_on_progress` takes *one* argument and *returns* the callback. So every progress tick
    # raised `TypeError` inside the seam's sampler, where it became an unretrieved task
    # exception, and the progress column stayed empty for the whole download. Passing the
    # partial is the kind of mistake that is invisible precisely because the callback is a
    # closure over state and both forms "look" like a bound function;
    # `test_create_app_hands_the_seam_a_live_progress_callback` calls what the factory handed
    # over, so it cannot come back.
    #
    # An injected `ripper` is left exactly as given: a test's fake already has whatever
    # behaviour it is asserting, and wrapping it in a real host to attach a callback would
    # defeat the injection.
    app.state.ripper = (
        ripper
        if ripper is not None
        else RipperHost(
            app.state.ripper_config_path, on_progress=_on_progress(app.state)
        )
    )
    app.state.supervisor = (
        supervisor
        if supervisor is not None
        else WrapperSupervisor(
            binary=resolved.wrapper_binary,
            base_dir=resolved.wrapper_base_dir,
            host=resolved.wrapper_host,
            port=resolved.wrapper_port,
            log_sink=lambda line: _log(app.state, line),
        )
    )
    # Set before the lifespan runs, and read by every status path, so "the wrapper is not
    # running" can always be answered with the reason it is not.
    app.state.startup_error = None
    app.state.pending_2fa = None
    app.state.stopping = asyncio.Event()
    app.state.scheduler: asyncio.Task | None = None
    app.state.degraded_roots: tuple[str, ...] = ()
    # Which job the scheduler is running, and the loop to hop back to. Both are read by the
    # progress callback, which is called from upstream's transfer loop on a worker thread and
    # therefore cannot touch the store or the broker directly. `None` whenever nothing is
    # running, which is the check that keeps a late reading from being written to a job id
    # that has since been cancelled.
    app.state.current_job: int | None = None
    app.state.loop: asyncio.AbstractEventLoop | None = None
    # The last readiness answer, and it is **never read as an answer** -- `scheduler_loop`
    # probes before every claim, which is the whole of I5. It exists only so a caller that
    # wants to know "is the wrapper up?" between claims can ask without a round-trip, and it
    # is set on every probe so it cannot go stale without anyone noticing.
    app.state.cached_problem: str | None = None
    # The session generation, bumped by logout (I1). A session here is a boolean with no
    # server-side table, so "which sessions are alive" is this one integer: incrementing it
    # retires every token ever issued, which is what makes logout a revocation rather than a
    # request to the browser. It is per-`app` rather than module scope because two apps in one
    # process (a test and a REPL) must not share it.
    app.state.session_generation = 0
    app.state.templates = api.build_templates()
    app.state.jobs_counts = partial(_jobs_counts, app.state)
    app.state.run_one = partial(run_one, app.state)
    app.state.run_pool = partial(run_pool, app.state)
    app.state.ripping_adam_ids = {}
    app.state.scheduler_loop = partial(scheduler_loop, app.state)

    api.install(app)
    return app


async def _drain(state) -> None:
    """Stop the scheduler and wait for the job it is running.

    Awaiting, not cancelling. `RipperHost.close()` refuses while a rip is in flight and its
    reason is sound -- the process working directory it holds is what upstream resolves the
    FairPlay template and the download directory against, and restoring it mid-rip loses the
    template and writes to the wrong tree with no error. So the scheduler is given
    `DRAIN_TIMEOUT_SECONDS` to finish on its own, and only then cancelled -- and a
    cancellation is safe, because the seam releases its in-flight counter in a `finally`.
    """
    state.stopping.set()
    task, state.scheduler = state.scheduler, None
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        # The job outlived its grace. Cancelling is safe for the host -- the seam releases
        # its in-flight counter in a `finally` -- and `run_one` turns the cancellation into
        # `cancelled` rather than leaving the row `running` with nothing to update it.
        task.cancel()
    # Every exit path awaits the task, and a `CancelledError` out of it must not escape into
    # the lifespan: that would abort the shutdown *before* the host is closed, which is the
    # one ordering this function exists to get right. `BaseException` rather than
    # `Exception` because `CancelledError` is not an `Exception`.
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _jobs_counts(state) -> dict:
    """How many jobs are in each state. One table read, no cache.

    `total` is the sum rather than a separate `COUNT(*)`, so it cannot disagree with the
    parts if a status is ever added to the store's vocabulary without being added here.
    """
    counts: dict[str, int] = {}
    jobs = state.jobs.list()
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    counts["total"] = len(jobs)
    return counts


def _log(state, line: str) -> None:
    """The wrapper's own output, onto the stream and onto stderr.

    **The supervisor scrubs this before it gets here** (R6: credentials and tokens are
    replaced before the line leaves the child's pipe), which is why the hub can display a
    child's log lines at all. It is published on the jobs channel so one SSE stream carries
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

    **Single worker, and it is not a default that can be overridden by accident.** Everything
    the app owns lives on `app.state`: the broker, the job store, the leaf registry, the
    scheduler and the session generation. Two workers would be two brokers (so an SSE
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
