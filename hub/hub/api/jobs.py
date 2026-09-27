"""The queue: enqueue, list, cancel, retry, and the live stream.

This is where a request becomes queue rows, and there are two places where getting it wrong
is silent:

**The answer is four keys, not three.** The contract is
`{created[], skipped[], deduplicated[]}`
and `JobStore.create_batch` implements that. But `create_batch` *applies the leaves it could
and then raises* `ValueError` on one it could not, because a 19-track album must not lose
19 tracks to one bad `adam_id`. So the handler catches it, reads back what actually landed
**by `parent_url`**, and adds a `rejected` list naming the one that did not. A 500 here
would be a 19-track album queued behind an error the user cannot see.

**The read-back is filtered by `parent_url` and not by `parent_id`.** `parent_id` is the
`job` table's self-reference and `create_batch` has no such parameter, so every row has
`NULL` in it; `list(parent_id=None)` therefore means *no filter*, and using it to answer
"what did this request create?" would return the user's entire queue and report all of it as
this request's work. `store.list(parent_url=...)` is the only filter that means what it says.

Nothing here decides whether a track is already on disk. That is the scheduler's, at
execution time, in `hub.app` -- because a queued job can sit long enough for the file to be
deleted underneath it, and a second filesystem check at enqueue time would put two dedup
checks with different timings into the codebase for them to disagree. `skipped` is
therefore always empty in this response, and it is here because the contract above has it.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from hub.api import fail, guarded
from hub.events import SubscriberOverrun
from hub.jobs import Job, Leaf
from hub.resolver import ResolveError, expand
from hub.vendor import parse_apple_music_url

router = guarded()

#: The channel every job event and every wrapper log line is published on. One channel, not
#: two, because the stream has to deliver both in the order they happened and a snapshot
#: interleaved between two subscribers' live halves cannot be placed correctly by a reader.
JOBS_CHANNEL = "jobs"

#: `EventBroker.subscribe` replays up to `HISTORY` past frames, which for this channel are
#: older than the snapshot the stream starts with. Sending them would render a stale queue
#: and then fix it, and sending the transition twice would be worse. See `_job_frames`.
SNAPSHOT_KIND = "snapshot"

#: Upstream's `Codec` (`src/types.py`), restated because the boundary test forbids importing
#: it. A duplicated constant, so `test_the_codec_set_matches_the_clients` compares it against
#: the real file and skips rather than failing when there is no vendor checkout.
CODECS: frozenset[str] = frozenset(
    {"alac", "ec3", "ac3", "aac-binaural", "aac-downmix", "aac", "aac-legacy"}
)

#: The `url_type` column's five values, which is also `hub.jobs.PARENT_TYPES`. Duplicated
#: for the same reason and checked in `test_the_codec_set_matches_the_clients`'s sibling
#: assertion below.
PARENT_TYPES: frozenset[str] = frozenset(
    {"song", "album", "artist", "playlist", "music-video"}
)


def job_to_dict(job: Job) -> dict:
    """A `Job` as JSON: `asdict`, and nothing added.

    `asdict` rather than a hand-written mapping, so a column added to the `job` table
    appears here without anyone remembering. `force` is an `int` in the row and a `bool` in
    the dataclass, and the dataclass already converted it -- sending `1`/`0` would make the
    browser's `if (job.force)` work by accident and its rendering show the wrong thing.

    **No `is_music_video` key.** It was here as a hardcoded `False`, which is the one value a
    music-video job must never report: the `job` table has no column for it (`Leaf`'s own
    docstring says so, and it is not persisted), so this line was the only reason the key
    appeared in the API at all, and it was a fabrication. Nothing renders it today, but it is
    in the public JSON contract and a Phase 2 filter over `/api/jobs?type=music-video` would
    have been misled by it.

    The value is *available* for a caller that needs it -- `app.state.leaves` holds the leaf
    for every job this process queued, and the leaf knows. Round-tripping it through a `Leaf`
    that may be absent (a job from a previous process) or wrong (a failed re-expansion) is a
    worse answer than its absence, so a consumer that needs the real value should ask the
    leaf registry. `test_the_job_contract_does_not_fabricate_is_music_video` holds the
    absence.
    """
    return asdict(job)


# --------------------------------------------------------------------------- #
# The leaf registry
# --------------------------------------------------------------------------- #
class LeafRegistry:
    """`job id -> Leaf`, for the leaves this process expanded.

    **Why it has to exist.** The `job` table stores `adam_id`, `title`, `codec` and
    `language` -- deliberately, because the filesystem is the source of truth for what is
    downloaded and a description of the track is operational state. But both consumers of a
    job need more than that: `RipperHost.run_song` needs the storefront, and the scheduler's
    duplicate check in `hub.app` needs the album name and the artist to render the file
    name. So the expansion is held here, from the request that made it until the job
    finishes.

    **What a miss means, and why it is a failure rather than a blank.** A row whose leaf this
    process never saw was enqueued by a previous hub process (or by hand). `hub.app`'s
    `_leaf_for` re-expands the parent URL in that case, which is why a restart is not a
    reason to lose a queue -- and when even that cannot produce the track, the job is failed
    with a message naming the id, so the user can re-submit it. Running it with blanks
    instead would be worse than useless: a blank album name normalizes to `""`, and
    `find_duplicate` refuses an empty key, so the check would silently pass every time.

    Bounded, because a dict keyed by job id that only shrinks when the scheduler runs would
    grow for the life of the process if the scheduler were not running. Oldest ids go first,
    which is the order the queue would run them in anyway.
    """

    def __init__(self, *, capacity: int = 4096) -> None:
        self._leaves: dict[int, Leaf] = {}
        self._capacity = capacity

    def get(self, job_id: int) -> Leaf | None:
        return self._leaves.get(job_id)

    def put(self, job_id: int, leaf: Leaf) -> None:
        self._leaves[job_id] = leaf
        while len(self._leaves) > self._capacity:
            del self._leaves[min(self._leaves)]

    def remember(self, job: Job, leaf: Leaf) -> None:
        """Hold `leaf` for `job`. The pair is given rather than derived from the batch.

        `BatchResult.created` is a list of ids in the order the inserts succeeded, and a
        batch can hold the same `adam_id` twice, so the only unambiguous mapping back to
        leaves is by the key the store itself uses. The caller does that lookup.
        """
        self.put(job.id, leaf)

    def forget(self, job_id: int) -> None:
        self._leaves.pop(job_id, None)

    def __len__(self) -> int:
        return len(self._leaves)


# --------------------------------------------------------------------------- #
# POST /api/jobs
# --------------------------------------------------------------------------- #
class _JobsBody(BaseModel):
    urls: list[str] = []
    codec: str = ""
    language: str = ""
    force: bool = False


def parent_type_for(url: str, leaf_count: int) -> str:
    """The `url_type` stored for a batch, from the URL.

    Asked *before* the expansion because `create_batch` requires a type and `expand` does not
    return one. Two sources, in order:

    1. `hub.vendor.parse_apple_music_url` -- the resolver's own parser, reached through the
       one module allowed to, so this is not a second definition of what an Apple URL means.
       It covers every `music.apple.com` link, including a share link's `?i=<songId>`, which
       is a *song* even though its path says `album`.
    2. The path segment, for the pre-2015 `itunes.apple.com` links that parser does not
       match. `AppleMusicURL.parse_url` matches `music.apple.com` only, and the resolver
       follows the redirect for those, so the link is accepted and its kind has to be read
       from the URL the user pasted -- which carries the same five segment names.

    With neither, the leaf count decides: one leaf came back for one URL, so it named one
    thing. That is evidence rather than a guess, and it is only reached for a URL the modern
    parser did not recognise *and* whose path is not one of the five.

    A wrong answer here is a mislabelled queue row and nothing worse: the scheduler routes on
    `Leaf.is_music_video`, which comes from the resolver, not on this string.
    """
    parsed = parse_apple_music_url(url)
    kind = getattr(parsed, "type", None) if parsed is not None else None
    if kind in PARENT_TYPES:
        return str(kind)
    segment = _path_segment(url)
    if segment in PARENT_TYPES:
        return segment
    return "song" if leaf_count == 1 else "album"


def _path_segment(url: str) -> str | None:
    """The first path segment that names a kind, or `None`.

    `urlparse` rather than a split, so a query string cannot be mistaken for one -- and only
    segments that are in the closed set count, so `https://example.com/album/1` answers
    `None` and the caller falls through to the evidence-based answer instead of storing a
    URL the resolver will refuse anyway.
    """
    from urllib.parse import urlparse

    for part in urlparse(url).path.split("/"):
        if part in PARENT_TYPES:
            return part
    return None


@router.post("/api/jobs")
async def create_jobs(request: Request, body: _JobsBody | None = None) -> Response:
    """Expand each URL and enqueue every track it names.

    The whole of the *enqueue* half of dedup. The other half -- "is it already on disk" -- is
    per-file and happens at execution time in `hub.app`, so `skipped` is always empty here
    and the response says so by carrying the key.

    Every URL is attempted, and one bad URL does not discard the others. A user pasting three
    links and getting an error for the second one should still have the first and third
    queued, so each is expanded in its own try and the failures are reported per URL.
    """
    state = request.app.state
    payload = body or _JobsBody()

    if not payload.urls:
        return fail(400, "no URLs were given, so there is nothing to queue. `urls` is a list "
                         "of Apple Music links -- a song, album, playlist, artist or music "
                         "video.")
    if payload.codec not in CODECS:
        return fail(
            400,
            f"codec {payload.codec!r} is not one this client can rip "
            f"({', '.join(sorted(CODECS))}). A value outside the set would be stored in a "
            f"NOT NULL column and fail once, inside the client's retry loop, long after "
            f"the request that carried it.",
        )

    web_api = state.ripper.web_api
    if web_api is None:
        return fail(
            503,
            "the downloader client is not started, so there is nothing to expand a URL "
            "against. This is a startup failure, not a bad request; the log says why.",
        )

    language = (payload.language or "").strip() or _default_language(state)
    if not language:
        return fail(
            400,
            "no language was given and the client's own config does not name one. It is "
            "asked for rather than defaulted: the library on disk was written with "
            "whatever this client was configured with, and metadata in a different "
            "language would never match a file name.",
        )

    created: list[int] = []
    skipped: list[int] = []
    deduplicated: list[int] = []
    rejected: list[str] = []
    problems: list[dict] = []

    for url in payload.urls:
        cleaned = url.strip() if isinstance(url, str) else ""
        try:
            leaves = await expand(cleaned, codec=payload.codec, language=language, web_api=web_api)
        except ResolveError as exc:
            problems.append({"url": url, "detail": str(exc)})
            continue

        if not leaves:
            problems.append(
                {"url": url, "detail": "that URL named no tracks -- an empty album, playlist "
                                       "or artist, which is an answer rather than a failure."}
            )
            continue

        before = {job.id for job in state.jobs.list(parent_url=cleaned)}
        # Per-URL, not cumulative (I6). The running totals above are what the *response*
        # reports; what a stream frame carries is what *this* URL queued, because a tab
        # merges frames and would otherwise log ids it has already seen. The two frames of a
        # two-URL request used to carry all four ids each, and `app.js` logged ids 3 and 4 on
        # the first frame it saw.
        this_created: list[int] = []
        this_deduplicated: list[int] = []
        try:
            result = state.jobs.create_batch(
                cleaned, parent_type_for(cleaned, len(leaves)), leaves, force=payload.force
            )
        except ValueError as exc:
            # The partial application is the point. `create_batch` wrote the leaves before
            # this one and will not attempt the ones after it, so what is on disk is not
            # what `result` would have said and `result` does not exist.
            _record_landed(state, cleaned, before, created, deduplicated)
            this_created = [job_id for job_id in created if job_id not in before]
            this_deduplicated = [
                job_id for job_id in deduplicated if job_id not in before
            ]
            rejected.append(_rejected_name(leaves, cleaned))
            problems.append({"url": url, "detail": str(exc)})
        else:
            created.extend(result.created)
            deduplicated.extend(result.deduplicated)
            this_created = list(result.created)
            this_deduplicated = list(result.deduplicated)
            _remember_leaves(state, leaves, result.created)

        _publish_batch(
            state, url=cleaned, created=this_created, deduplicated=this_deduplicated
        )

    body_out: dict[str, Any] = {
        "created": created,
        "skipped": skipped,
        "deduplicated": deduplicated,
        "rejected": rejected,
    }
    if problems:
        # 200 with the detail attached, not 4xx: 19 tracks were queued and a 500 would
        # throw that away. The status is only raised when *nothing* was queued and something
        # went wrong, which is the case where the user has to be told to fix something.
        body_out["problems"] = problems
        if not created and not deduplicated:
            first = problems[0]
            return JSONResponse(
                status_code=400,
                content={
                    **body_out,
                    "detail": first["detail"],
                    "url": first["url"],
                },
            )
    return JSONResponse(body_out)


def _default_language(state) -> str:
    """`region.language` from the client's own config, or `""` if it is not started."""
    language = getattr(state.ripper, "region_language", None)
    return language.strip() if isinstance(language, str) else ""


def _record_landed(
    state, url: str, before: set[int], created: list[int], deduplicated: list[int]
) -> None:
    """Classify what `create_batch` managed to apply before it raised.

    `before` is the set of ids this url already had, snapshotted before the call. After the
    `ValueError` the result object does not exist, so this is the only way to know what
    landed: a row that is not in `before` was inserted by this call, and a row that *is* was
    there because some leaf folded into it.

    The status filter on the second half is not redundant with the snapshot. A row for this
    url can be `done` or `failed`, and the partial index does not cover those -- so a new
    leaf with the same key would have created a *second* row rather than folding, and
    counting the old terminal one as `deduplicated` would report an id the user never queued
    this time.

    **The read is by `parent_url` and nothing else**, and the module docstring is the reason:
    `parent_id` is a self-reference nothing writes, so `list(parent_id=None)` means "no
    filter" and would return the user's entire queue as this request's work.

    **A holder in another batch is not recoverable, and `deduplicated` is a lower bound on a
    refused batch.** The fold is recorded on the holder's row, which may belong to a request
    for a completely different URL, and there is no column on it saying which leaf put it
    there. So a refused batch reports the folds it can see and the user is told what actually
    happened to the rest; `create_batch`'s own message is in `problems` either way.
    """
    for job in state.jobs.list(parent_url=url):
        if job.id in created or job.id in deduplicated:
            continue
        if job.id in before:
            if job.status in ("queued", "waiting", "running"):
                deduplicated.append(job.id)
        else:
            created.append(job.id)


def _rejected_name(leaves: list[Leaf], url: str) -> str:
    """A name for the leaf `create_batch` refused, so the response is actionable.

    The refusal is always about a leaf's *dedup key* -- `create_batch` raises `ValueError` for
    exactly three things (a bad `parent_type`, a blank `parent_url`, and an unusable
    `(adam_id, codec)`), and the first two are checked here before the call, so an empty or
    whitespace-only `adam_id` or `codec` is the only one that can reach it. That is the same
    rule `JobStore._check_key` applies, restated in three lines rather than imported: the
    store's own message is in `problems` and is the authority on *why*, and this only has to
    say *which*.

    **The offending leaf's own title, not the batch's first one.** A 19-track album is not
    helped by being told track 1 failed, and a title is the only handle a user has on a
    specific track. When the title is blank too, the position and the URL are the honest
    description: "track 7 of <url>".
    """
    for index, leaf in enumerate(leaves):
        unusable = any(
            not isinstance(getattr(leaf, field), str) or not getattr(leaf, field).strip()
            for field in ("adam_id", "codec")
        )
        if unusable:
            return leaf.title.strip() or f"track {index + 1} of {url}"
    return f"a track of {url}"


def _remember_leaves(state, leaves: list[Leaf], created_ids: list[int]) -> None:
    """Pair the created ids with their leaves and hold them for the scheduler.

    Matched on `(adam_id, codec)` -- the store's own dedup key -- because `created` is in
    leaf order but a batch may hold the same key twice, and the second one folded into
    `deduplicated` rather than creating a row. So among the created ids each key is unique,
    and the match is exact rather than positional.
    """
    by_key = {(leaf.adam_id, leaf.codec): leaf for leaf in leaves}
    for job_id in created_ids:
        job = state.jobs.get(job_id)
        if job is None:
            continue
        leaf = by_key.get((job.adam_id, job.codec))
        if leaf is not None:
            state.leaves.remember(job, leaf)


def _publish_batch(state, *, url: str, created: list[int], deduplicated: list[int]) -> None:
    """Tell every open tab what *this* URL queued. Per-URL lists, never the running total.

    Published per URL rather than once at the end, so a long multi-album request shows its
    tracks as they are queued instead of after the last one finishes expanding. And the lists
    are the ones for this url, not the caller's cumulative ones: a tab that merges frames
    would otherwise see ids 3 and 4 on the first frame of a two-URL request and log them
    again when the second frame arrived. The docstring said the opposite of what the call did
    (I6), so both the call site and this line now say per-URL.
    """
    state.broker.publish(
        JOBS_CHANNEL,
        {
            "kind": "batch",
            "url": url,
            "created": created,
            "deduplicated": deduplicated,
        },
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
@router.get("/api/jobs")
async def list_jobs(request: Request, status: str | None = None, parent: str | None = None) -> dict:
    """The queue, oldest first, optionally filtered by `?status=` and `?parent=`.

    `parent` is a *url*, matching what `create_batch` wrote. The `parent_id` self-reference
    is populated by nothing, so a filter on it would be a filter on nothing --
    see the module docstring.
    """
    state = request.app.state
    try:
        jobs = state.jobs.list(status=status, parent_url=parent)
    except ValueError as exc:
        return fail(400, str(exc))
    return {"jobs": [job_to_dict(job) for job in jobs]}


@router.get("/api/jobs/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: a snapshot, then every change.

    Plain `text/event-stream` with one `data:` field per frame, which is what
    `EventBroker.frame` produces. A named `event:` is not used: a browser's default
    `onmessage` handler is the one that has to work without configuration, and
    `EventSource` reconnects on its own when the stream ends -- which is what an overrun
    below relies on.
    """
    state = request.app.state
    return StreamingResponse(
        _job_frames(state),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # nginx buffers a proxied response by default, which turns a live stream into
            # one 4 KB block delivered whenever the buffer happens to fill.
            "X-Accel-Buffering": "no",
        },
    )


async def _job_frames(state):
    """The snapshot, then the live stream, with nothing before the snapshot and none after.

    **The ordering problem.** `EventBroker.subscribe` deliberately replays the channel's
    backlog so a tab opened late renders the current queue -- and that backlog is up to 50
    frames of *history*, which here describes a queue that has moved on. Emitting it would
    render a stale state and then correct it; emitting the snapshot and then the backlog
    would render it twice.

    So the first frame out is the first `{"kind": "snapshot"}` and everything before it is
    dropped, and everything after it is delivered untouched. The snapshot is published
    *after* the iterator is created, so the subscription is already registered when the
    publish happens: `subscribe` reads its backlog and registers with no `await` between
    them, so on one event loop no message can fall between the two halves. That is the
    broker's own invariant; this only has to not break it.

    `SubscriberOverrun` ends the response rather than being swallowed. The broker's frames
    are stale by the time it raises, and its own docstring says the decision belongs here:
    `EventSource` reconnects, the client gets a fresh snapshot, and nothing is delivered
    half-out-of-order.
    """
    # A short reconnect delay, as an SSE field rather than as a comment, so a client that
    # drops does not come back in the browser's default 3 s and re-take the snapshot.
    yield "retry: 2000\n\n"

    messages = state.broker.subscribe(JOBS_CHANNEL)
    state.broker.publish(JOBS_CHANNEL, _snapshot(state))

    seen_snapshot = False
    try:
        async for frame in messages:
            if not seen_snapshot:
                if _kind_of(frame) != SNAPSHOT_KIND:
                    continue
                seen_snapshot = True
            yield frame
    except SubscriberOverrun:
        # The connection is closed, not kept open with stale frames in it. EventSource
        # reconnects on its own and gets a new snapshot from the top of this function.
        return
    finally:
        # `async for` does not close the iterator it is abandoned on, and the broker's
        # unsubscribe lives in the inner generator's `finally`. Without this a closed tab
        # would stay a subscriber for the life of the process, and
        # `broker.subscriber_count` -- the only thing that can see it -- would climb.
        with suppress(Exception):
            await messages.aclose()


def _kind_of(frame: str) -> str | None:
    """The `kind` of an SSE frame, or `None` if it is not one of ours.

    Decoded rather than string-matched, because the frames are JSON built by
    `EventBroker.frame` and a substring test on them would break the day a job title
    contained the text being matched.
    """
    if not frame.startswith("data: "):
        return None
    try:
        payload = json.loads(frame[len("data: ") :].strip())
    except ValueError:
        return None
    return payload.get("kind") if isinstance(payload, dict) else None


def _snapshot(state) -> dict:
    """The queue as it is right now, from a real `list()`.

    **Not a fabricated empty queue.** A tab that connects to a channel nothing has been
    published on would otherwise be handed `{"jobs": []}`, which is indistinguishable from
    a real "the queue is empty" and reads as good news. `subscribe` waits rather than
    inventing, and the one snapshot in this stream comes from the table.
    """
    return {
        "kind": SNAPSHOT_KIND,
        "jobs": [job_to_dict(job) for job in state.jobs.list()],
    }


# POST /api/jobs/requeue, DELETE /api/jobs/finished
# --------------------------------------------------------------------------- #
# **Declared above the `{job_id}` routes on purpose.** FastAPI matches in declaration
# order, so a `POST /api/jobs/requeue` declared after `POST /api/jobs/{job_id}/retry`
# would still be fine -- but `GET /api/jobs/requeue` would be read as `job_id="requeue"`
# and answered 422 by the int coercion. The literal paths go first so that can never
# happen, whatever is added below.

#: The scopes a caller may name, rather than a list of statuses. "everything except
#: done" is the concept a user has; `{"queued","waiting","failed","skipped","cancelled"}`
#: is the implementation of it, and a caller that could pass statuses directly would
#: eventually pass `running`. `JobStore.requeue` refuses that anyway, but the vocabulary
#: belongs at the boundary.
REQUEUE_SCOPES: dict[str, frozenset[str]] = {
    "failed": frozenset({"failed"}),
    "unfinished": frozenset({"queued", "waiting", "failed", "skipped", "cancelled"}),
}


class _RequeueBody(BaseModel):
    scope: str = "failed"


@router.post("/api/jobs/requeue")
async def requeue_jobs(request: Request, body: _RequeueBody | None = None) -> Response:
    """Put a set of jobs back on the queue, and say which ones could not go back.

    **Both lists are in the answer, and that is the contract.** `refused` holds rows whose
    `(adam_id, codec)` another active job already holds -- `job_active_dedupe` refusing,
    which means the track is already on its way. Reporting them is what lets the UI say
    "2 queued, 1 already running" instead of silently showing a queue that does not
    contain what the user asked for.

    The store, not this handler, decides what is refused: it is the index that refuses,
    and `JobStore.requeue` reuses the same `_is_dedupe_violation` discrimination
    `create_batch` uses, so a non-dedupe integrity failure still raises rather than
    being reported as a duplicate.
    """
    state = request.app.state
    scope = (body.scope if body else "failed")
    statuses = REQUEUE_SCOPES.get(scope)
    if statuses is None:
        return fail(
            400,
            f"{scope!r} is not a requeue scope. Use one of: "
            f"{', '.join(sorted(REQUEUE_SCOPES))}.",
        )
    result = state.jobs.requeue(statuses)
    for job_id in result.requeued:
        _publish_job(state, job_id)
    return {"requeued": result.requeued, "refused": result.refused}


@router.delete("/api/jobs/finished")
async def delete_finished_jobs(request: Request) -> Response:
    """Remove every finished row, and forget the leaves that went with them.

    **Irreversible, and the answer says how much of it there was.** What is lost is the
    record that a track was attempted -- the title, the error text and the `skip_reason`
    evidence paths. The file is not touched and nothing re-downloads: the filesystem is
    the source of truth for what is on disk, and the execution-time dedup check
    reads the library rather than this table.

    A `running` row is not a finished row and survives, so a bulk delete issued while
    something is downloading cannot pull the ground out from under it.

    The leaves are forgotten because `LeafRegistry` is in memory and has no bulk clear:
    without this the registry would grow by one entry per deleted row for the life of the
    process, holding `Leaf` objects for jobs that no longer exist.
    """
    state = request.app.state
    deleted = state.jobs.delete_finished()
    for job_id in deleted:
        state.leaves.forget(job_id)
    return {"deleted": len(deleted)}


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: int) -> Response:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        return fail(404, f"no job with id {job_id}.")
    return job_to_dict(job)


@router.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: int) -> Response:
    """Cancel a queued job.

    A `running` job is refused rather than raced. There is no way to interrupt a rip that
    is inside `rip_song` -- upstream owns the transfer and its partial file -- so a delete
    that appeared to work would leave a file being written for a row that says `cancelled`.
    409 is the honest answer, and it is what makes the UI offer "cancel" only where it works.
    """
    state = request.app.state
    job = state.jobs.get(job_id)
    if job is None:
        return fail(404, f"no job with id {job_id}.")
    if job.status == "running":
        return fail(
            409,
            f"job {job_id} is running, and a rip in progress cannot be interrupted: the "
            f"client owns the transfer and its partial file. Wait for it to finish, then "
            f"delete the row.",
        )
    state.jobs.mark(job_id, "cancelled")
    state.leaves.forget(job_id)
    _publish_job(state, job_id)
    return job_to_dict(state.jobs.get(job_id))


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int) -> Response:
    """Put a finished job back on the queue.

    A queued, waiting or running job is refused with 409: the partial unique index already
    holds its `(adam_id, codec)`, so re-queueing it is either a no-op or an
    `IntegrityError`, and answering 409 names the real reason.

    `error` is deliberately **not** cleared. `claim_next` clears it when the job actually
    starts again, so a job the user never retries still says why it failed, and one that is
    retried shows the old reason until the new attempt begins. `finished_at` is cleared by
    `mark` itself, which is what keeps "finished" a function of the status.
    """
    state = request.app.state
    job = state.jobs.get(job_id)
    if job is None:
        return fail(404, f"no job with id {job_id}.")
    if job.status in ("queued", "waiting", "running"):
        return fail(
            409,
            f"job {job_id} is {job.status}, not finished, so there is nothing to retry.",
        )
    state.jobs.mark(job_id, "queued")
    _publish_job(state, job_id)
    return job_to_dict(state.jobs.get(job_id))


def _publish_job(state, job_id: int) -> None:
    """One job's current row, for the stream. Read from the store, not from a local copy.

    `hub.app` imports this rather than keeping its own, because two functions that both
    publish "the job as it is now" is two places for a future column to be forgotten in. The
    read is a `get()` because `mark` computes `finished_at` and the store is the only place
    that knows what it set: a published row that disagrees with the table is how a queue ends
    up showing a download that finished seconds ago as still running.
    """
    job = state.jobs.get(job_id)
    if job is not None:
        state.broker.publish(JOBS_CHANNEL, {"kind": "job", "job": job_to_dict(job)})
