"""The job store, its queue dedup, and the event broker.

The first seven tests came first, character for character. The rest exist because
"it returned 2 and 1" is also what a wrong implementation returns most of the time.

**What each section is for.** The schema itself -- the DDL, the partial index, the three
pragmas -- is *not* asserted here, because asserting it needs a raw connection and this
suite has never reached into an object's internals. It is asserted where a raw connection is
the right tool, by a check that prints `sqlite_master`, the index definition, and the
pragmas. What is pinned here is everything the schema exists to *do*:

- the dedup key is `(adam_id, codec)` over active jobs, and it deliberately excludes
  `language`, `force` and `is_music_video` (three separate tests, because getting one of
  them wrong is a silent skip or a silent double download);
- the index is **partial**: two `done` rows may share a key. A plain
  `UNIQUE (adam_id, codec)` would refuse the second, and `create_batch` would report it as
  "deduplicated" against a job that is never going to run -- the download would vanish with
  no error. That is the one failure this module cannot have, so it gets its own test built
  entirely out of the public API;
- a key that carries no identity is refused, because an empty string collides with every
  other empty string and a NULL never collides at all in a SQLite unique index;
- `claim_next` is exclusive **under real concurrency**. The brief's version is a sequential
  loop over one connection, which passes against a read-then-write implementation just as
  happily as against the atomic one; only threads can tell them apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime
from pathlib import Path

import pytest

from hub.events import HISTORY, SUBSCRIBER_QUEUE_SIZE, EventBroker, SubscriberOverrun
from hub.jobs import (
    ACTIVE_STATUSES,
    JOB_STATUSES,
    TERMINAL_STATUSES,
    IllegalTransition,
    JobNotFound,
    JobStore,
    Leaf,
)

# --- the first tests, verbatim ------------------------------------------------
#
# Character for character, including two assignments (`a`, `res`) that nothing reads. The
# only thing appended to any of them is a suppression marker for F841 on those two lines, so
# that this file lints as clean as the rest of the suite; no value, assertion or name was
# changed.


def leaf(adam_id="1", codec="alac"):
    return Leaf(adam_id=adam_id, title="t", album_name="A", artist_name="B",
                codec=codec, language="ja", url="https://music.apple.com/jp/song/x/1",
                storefront="jp")


def test_create_batch_deduplicates_identical_leaves(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1"), leaf("1"), leaf("2")], force=False)
    assert len(res.created) == 2
    assert len(res.deduplicated) == 1


def test_queue_dedup_ignores_language_and_force(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    a = store.create_batch("u", "album", [leaf("1")], force=False)  # noqa: F841
    b = store.create_batch("u", "album", [Leaf(**{**vars(leaf("1")), "language": "en-US"})], force=True)
    assert b.created == [] and len(b.deduplicated) == 1


def test_queue_dedup_key_includes_codec(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1", "alac")], force=False)
    res = store.create_batch("u", "album", [leaf("1", "ec3")], force=False)
    assert len(res.created) == 1


def test_a_finished_job_frees_the_dedupe_slot(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1")], force=False)  # noqa: F841
    job = store.claim_next()
    store.mark(job.id, "done")
    again = store.create_batch("u", "album", [leaf("1")], force=False)
    assert len(again.created) == 1


def test_waiting_jobs_hold_the_dedupe_slot(tmp_path):
    # 'waiting' is in the index so a token-blocked job is not re-run
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1")], force=False)
    job = store.claim_next()
    store.mark(job.id, "waiting")
    assert store.create_batch("u", "album", [leaf("1")], force=False).created == []
    assert store.resume_waiting() == 1


def test_claim_next_is_exclusive(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf(str(i)) for i in range(3)], force=False)
    a = store.claim_next()
    b = store.claim_next()
    assert a.id != b.id


async def test_broker_delivers_to_a_late_subscriber_the_current_snapshot():
    broker = EventBroker()
    broker.publish("jobs", {"kind": "snapshot", "jobs": []})
    got = []
    async for chunk in broker.subscribe("jobs"):
        got.append(chunk)
        break
    assert '"snapshot"' in got[0] and got[0].startswith("data: ")


# --- the interfaces `ripper_host.py` and `resolver.py` import ---------------
#
# Pinned as data rather than as behaviour: renaming a field breaks a module written against
# this one in another task, and it breaks at the import site with nothing of ours to explain
# why.


def test_the_leaf_fields_are_the_ones_the_seam_and_the_resolver_import():
    assert tuple(vars(leaf())) == (
        "adam_id",
        "title",
        "album_name",
        "artist_name",
        "codec",
        "language",
        "url",
        "storefront",
        "is_music_video",
    )


def test_a_leaf_is_a_music_video_only_when_something_says_so():
    assert leaf().is_music_video is False
    assert Leaf(**{**vars(leaf()), "is_music_video": True}).is_music_video is True


def test_a_leaf_cannot_be_edited_after_it_has_been_deduplicated():
    # The resolver builds leaves, hands them to the store, and goes on using them. A Leaf
    # that can be edited in between is a Leaf whose key can change after it was queued.
    with pytest.raises(FrozenInstanceError):
        leaf().title = "other"


def test_the_leaf_is_not_slotted_so_vars_still_works():
    # `test_queue_dedup_ignores_language_and_force` is one of those and it copies a leaf
    # with `vars()`. `slots=True` removes `__dict__` and makes that a TypeError, so the
    # dataclass is deliberately plain. Asserted because the symptom it causes is a TypeError
    # in a test that reads like a typo, several files away from the cause.
    assert isinstance(vars(leaf()), dict)
    assert vars(leaf())["adam_id"] == "1"


def poison_inserts(path: Path, body: str) -> JobStore:
    """A store whose every INSERT fails the way `body` says, and a queue already holding a key.

    The only way to reach a failure branch that the public API cannot reach is to make the
    statement fail, and a SQLite trigger is how that is done from outside: a second
    `sqlite3.connect` on the same file can install one. Two details make it work, and both
    cost a store's worth of care:

    - The store is **reopened** after the trigger is created. SQLite hands a connection the
      statement it already compiled, and that compiled statement has no trigger in it, so a
      trigger installed after the first INSERT is invisible to it. Reopening compiles the
      statement fresh, with the trigger present. (This is also why a fault-injection device
      like this cannot be left in a test that is really about something else.)
    - The row that occupies the key is written by the store *before* the trigger exists,
      because the interesting case is a failure on a leaf whose key is **already held** -- a
      failure that a handler inferring "a duplicate" from "a holder exists" would report as a
      successful deduplication.

    `body` is the trigger's statement, so the caller chooses the error: `RAISE(ABORT, ...)`
    gives an `IntegrityError` that is not the dedup index, and a call to a function that does
    not exist gives an `OperationalError`, which is a `sqlite3.Error` but not an
    `IntegrityError` -- the shape a `database is locked` during enqueue has.
    """
    store = JobStore(path)
    store.create_batch("u", "album", [leaf("1")], force=False)
    trigger = sqlite3.connect(path)
    try:
        trigger.executescript(f"CREATE TRIGGER poison BEFORE INSERT ON job BEGIN {body} END;")
        trigger.commit()
    finally:
        trigger.close()
    store.close()
    return JobStore(path)


# --- deduplication ---------------------------------------------------------


def test_deduplicated_names_the_job_that_holds_the_slot(tmp_path):
    """The id in `deduplicated` is the *holder's*, so the UI can link to it.

    Which of the two ids matters, and neither reading is obviously right: the id of the
    dropped leaf would identify nothing, because no row exists for it. So it is the id of
    the active job that already holds the key -- including when the holder was created
    milliseconds earlier by a sibling leaf of the same call.
    """
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    second = store.create_batch("u", "album", [leaf("1")], force=False)
    assert second.deduplicated == first.created
    assert store.get(first.created[0]).status == "queued"
    assert len(store.list()) == 1


def test_a_leaf_repeated_within_one_batch_is_deduplicated_against_its_sibling(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1"), leaf("1"), leaf("1")], force=False)
    assert res.created == [res.deduplicated[0]]
    assert len(store.list()) == 1


def test_deduplicated_names_the_running_job_not_the_oldest_one(tmp_path):
    """I5: the holder lookup filters by status, and the filter is not redundant with the index.

    The index constrains which rows may *exist*; it says nothing about which of the rows that
    share a key over time a `fetchone()` should return, and that returns them in id order. So
    with job 1 `done` and job 2 `queued` for one key, an unfiltered lookup hands back **1** --
    the finished job -- and the UI renders "already queued as #1" for something that will never
    run again. The same failure the partiality test prevents on the write side, with no
    counterpart on the read side, which is why this is a test and not a comment.
    """
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    store.claim_next()
    store.mark(first.created[0], "done")
    # Job 1 is `done`, so the slot is free and this creates job 2, which stays `queued`.
    second = store.create_batch("u", "album", [leaf("1")], force=False)
    assert second.created == [2] and store.get(2).status == "queued"
    # Job 1 (done) and job 2 (queued) share the key, and only job 2 holds the slot.
    third = store.create_batch("u", "album", [leaf("1")], force=False)
    assert third.created == []
    assert third.deduplicated == [2]


def test_an_integrity_failure_that_is_not_the_dedup_index_is_raised(tmp_path):
    """I1: a `NOT NULL` violation on a leaf whose key is already held must not be swallowed.

    This is the branch that exists so a `deduplicated` entry always means "a row is running or
    waiting or queued for this key". Here the INSERT fails for an unrelated reason while a
    holder is present, so a handler that infers "so it was a duplicate" from "a holder exists"
    reports a successful deduplication and the user is shown a track that has no row at all.
    Asserted against the *raised* error, and against the queue being untouched afterwards, so
    the two ways to get it wrong -- returning a result, or writing a row -- both fail.
    """
    store = poison_inserts(
        tmp_path / "hub.db",
        "SELECT RAISE(ABORT, 'NOT NULL constraint failed: job.title');",
    )
    with pytest.raises(sqlite3.IntegrityError, match="job.title"):
        store.create_batch("u", "album", [leaf("1")], force=False)
    # The one row is the holder the setup wrote; the failed attempt added nothing.
    assert [job.id for job in store.list()] == [1]
    assert len(store.list()) == 1


def test_a_sqlite_error_that_is_not_an_integrity_error_is_raised(tmp_path):
    """I1, the dangerous half: broadening the handler to `sqlite3.Error` must still fail.

    A `database is locked` or a closed connection is a `sqlite3.Error` and not an
    `IntegrityError`, and it arrives here with a holder present just as readily. A handler
    that catches too much turns "the write did not happen" into "already queued", which is the
    one failure this module must not have: the user is told a download is coming and nothing
    is running. The trigger raises it by calling a function that does not exist, which SQLite
    rejects while preparing the statement.
    """
    store = poison_inserts(
        tmp_path / "hub.db", "SELECT this_function_does_not_exist();"
    )
    with pytest.raises(sqlite3.Error) as caught:
        store.create_batch("u", "album", [leaf("1")], force=False)
    assert not isinstance(caught.value, sqlite3.IntegrityError), (
        "this test's value is that the error is NOT an IntegrityError; if that changes, it is"
        " no longer testing the broadened-except case"
    )
    assert "this_function_does_not_exist" in str(caught.value)
    assert len(store.list()) == 1


def test_the_store_is_usable_after_a_rejected_insert(tmp_path):
    """An `IntegrityError` must roll the statement back, not poison the connection.

    In autocommit the failed statement is rolled back on its own -- which is the reason this
    store is opened with `isolation_level=None`, and the reason the dedup path works at all
    instead of leaving a transaction open that every later statement would trip over. Asserted
    because the whole dedup mechanism is downstream of it: if this ever regressed, the *second*
    duplicate in a batch would fail with "cannot start a transaction within a transaction".
    """
    store = poison_inserts(
        tmp_path / "hub.db",
        "SELECT RAISE(ABORT, 'NOT NULL constraint failed: job.title');",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_batch("u", "album", [leaf("1")], force=False)
    trigger = sqlite3.connect(tmp_path / "hub.db")
    try:
        trigger.execute("DROP TRIGGER poison")
        trigger.commit()
    finally:
        trigger.close()
    # Same store, no reopen: the connection survived and enqueues normally again.
    assert store.create_batch("u", "album", [leaf("9")], force=False).created


def test_the_index_is_partial_so_two_finished_jobs_may_share_a_key(tmp_path):
    """What a plain `UNIQUE (adam_id, codec)` would forbid, built from the public API.

    A track re-downloaded after a `failed` run, or re-queued after a `cancelled` one, is two
    rows with the same key and both of them outside the index's predicate. This is the
    difference between "the queue dedupes active work" and "the track can only ever be
    downloaded once in the life of the database", and only the second one is a bug.
    """
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    store.claim_next()
    store.mark(first.created[0], "failed", error="wrapper went away")
    second = store.create_batch("u", "album", [leaf("1")], force=False)
    store.claim_next()
    store.mark(second.created[0], "done")
    third = store.create_batch("u", "album", [leaf("1")], force=False)
    # Three rows, one key. The `failed` one and the `done` one could not both exist under a
    # plain `UNIQUE (adam_id, codec)`, and the third -- queued, same key -- could not exist
    # under it either, which would have made every re-download of a failed track
    # unreportable.
    assert [first.created, second.created, third.created] == [[1], [2], [3]]
    assert sorted(job.status for job in store.list()) == ["done", "failed", "queued"]


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_every_terminal_status_frees_the_dedupe_slot(tmp_path, status):
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    store.mark(store.claim_next().id, status)
    again = store.create_batch("u", "album", [leaf("1")], force=False)
    assert again.created and again.created != first.created


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
def test_every_active_status_holds_the_dedupe_slot(tmp_path, status):
    """The other half of the index predicate, which is the part that can bite.

    `queued` is the obvious case. `waiting` is the one the index calls out, and `running` is
    the one a naive `'status = 'queued''` pre-check would get wrong: a download in progress
    holds the key, so a second copy of the same URL pasted in during it is deduplicated
    rather than run alongside it.
    """
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    if status in ("running", "waiting"):
        store.mark(store.claim_next().id, status)
    else:
        store.mark(first.created[0], status)
    assert store.create_batch("u", "album", [leaf("1")], force=False).created == []


def test_the_holder_keeps_its_own_language_and_url(tmp_path):
    """A deduplicated batch changes nothing about the job it folded into.

    Otherwise "add this album" would silently rewrite the language of a download that is
    already queued or running, and the user would get a file in a language nobody asked for
    half way through the transfer.
    """
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("https://first/album/1", "album", [leaf("1")], force=False)
    store.create_batch("https://second/album/1", "album", [Leaf(**{**vars(leaf()), "language": "de-DE"})], force=False)
    (job,) = store.list()
    assert (job.parent_url, job.language) == ("https://first/album/1", "ja")


def test_force_is_stored_and_does_not_buy_a_second_slot(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1")], force=True)
    assert store.get(first.created[0]).force is True
    # `force` is deliberately not in the key: the index has no way to express it, and a
    # force that produced a second concurrent job for one track would be the opposite of what
    # the user asked for. It is passed through and honoured later, by the execution task.
    second = store.create_batch("u", "album", [leaf("1")], force=True)
    assert second.created == [] and second.deduplicated == first.created


def test_is_music_video_is_not_part_of_the_key(tmp_path):
    """A music video and a song for one adam_id are one queue entry, not two.

    `is_music_video` is a `Leaf` field with no `job` column behind it: it selects the
    Widevine path and nothing else, so it cannot reach
    the key. Said out loud here because it is the one field on a `Leaf` that a reader would
    expect to be a key component.
    """
    store = JobStore(tmp_path / "hub.db")
    video = Leaf(**{**vars(leaf()), "is_music_video": True})
    res = store.create_batch("u", "music-video", [video, leaf()], force=False)
    assert res.deduplicated == res.created and len(res.created) == 1


def test_a_batch_of_no_leaves_does_nothing(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [], force=False)
    assert (res.created, res.skipped, res.deduplicated) == ([], [], [])


def test_the_parent_url_and_type_are_stored_on_every_job(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("https://music.apple.com/jp/album/x/1", "album", [leaf("1"), leaf("2")], force=False)
    assert [(job.parent_url, job.parent_type) for job in store.list()] == [
        ("https://music.apple.com/jp/album/x/1", "album"),
        ("https://music.apple.com/jp/album/x/1", "album"),
    ]


# --- a key has to carry an identity ----------------------------------------


@pytest.mark.parametrize("field", ["adam_id", "codec"])
@pytest.mark.parametrize("value", ["", None])
def test_a_key_with_no_identity_in_it_is_refused(tmp_path, field, value):
    """Both failure directions of an empty key, and only one of them is obvious.

    `""` would collide with every other `""` -- including from a different album, a
    different codec and a different URL -- so unrelated tracks would deduplicate against each
    other. `None` is the subtler one: the schema allows `adam_id` to be NULL and a SQLite
    unique index treats every NULL as distinct, so a NULL row does not deduplicate against
    anything *and* does not stop a second NULL row from being created. The index would look
    present and simply not apply. `dedup.find_duplicate` refuses both for the same reason
    and in the same direction: a re-download is recoverable, a false skip is not.
    """
    store = JobStore(tmp_path / "hub.db")
    with pytest.raises(ValueError, match=field):
        store.create_batch("u", "album", [Leaf(**{**vars(leaf()), field: value})], force=False)
    assert store.list() == []


def test_a_refused_key_stops_the_batch_but_keeps_what_had_already_landed(tmp_path):
    """One bad leaf is refused loudly, and the leaves before it are not thrown away with it.

    A whole album arriving from an API with one unusable `adam_id` is a normal thing to
    happen. `create_batch` raises on it -- there is nowhere in a `BatchResult` to report a
    leaf that was neither created nor deduplicated, and inventing a fourth list for a case
    that should not occur is worse than a loud failure -- but it does not roll the batch back,
    because autocommit means the earlier leaves are already rows. The leaves *after* the bad
    one are not attempted, and that is the part the API layer has to know: it is the one
    caller of this, and it catches the `ValueError`, reads `list()`, and shows what is
    queued. A retry
    is safe, because the index makes the already-enqueued leaves come back as
    `deduplicated` rather than as duplicates.
    """
    store = JobStore(tmp_path / "hub.db")
    bad = Leaf(**{**vars(leaf("2")), "adam_id": ""})
    with pytest.raises(ValueError, match="adam_id"):
        store.create_batch("u", "album", [leaf("1"), bad, leaf("3")], force=False)
    assert [job.adam_id for job in store.list()] == ["1"]


@pytest.mark.parametrize("url", [None, "", "   "])
def test_a_url_that_identifies_nothing_is_refused(tmp_path, url):
    """I2: `parent_url` is validated, so a blank one is a `ValueError` and not a 500.

    Without the check it escapes as a bare `sqlite3.IntegrityError: NOT NULL constraint
    failed: job.url`, which is outside the one `except ValueError` the API layer is written
    around. The `url` column is not decoration: `GET /api/jobs?parent=` filters on it, so a
    batch with no usable one could neither be listed nor retried.
    """
    store = JobStore(tmp_path / "hub.db")
    with pytest.raises(ValueError, match="parent_url"):
        store.create_batch(url, "album", [leaf()], force=False)
    assert store.list() == []


def test_a_url_is_refused_before_any_leaf_is_touched(tmp_path):
    """The batch-level arguments are checked first, so a bad one costs nothing.

    Per-leaf keys are validated as the loop reaches them, and a refused leaf stops the batch
    (§ `test_a_refused_key_stops_the_batch_but_keeps_what_had_already_landed`). A bad URL is
    known before the first leaf, so it is refused before the first insert -- otherwise the
    same album would be half-enqueued and then fail, and the caller's retry would fold the
    half into `deduplicated` for a request that never had a URL.
    """
    store = JobStore(tmp_path / "hub.db")
    with pytest.raises(ValueError):
        store.create_batch(None, "album", [leaf("1"), leaf("2")], force=False)
    assert store.list() == []


def test_a_url_with_only_surrounding_whitespace_is_refused_not_stored(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    with pytest.raises(ValueError, match="parent_url"):
        store.create_batch("  \n ", "album", [leaf()], force=False)
    assert store.list() == []


def test_an_unknown_parent_type_is_refused(tmp_path):
    """A closed set, not a free string.

    Five types, and the type is what the API layer switches on to decide between
    resolving a track and resolving an album. A typo would store cleanly and be rendered as an
    unrecognised kind forever, so the refusal happens where the value came from.
    """
    store = JobStore(tmp_path / "hub.db")
    with pytest.raises(ValueError, match="album"):
        store.create_batch("u", "albume", [leaf()], force=False)
    assert store.list() == []


# --- claiming --------------------------------------------------------------


def test_claim_next_on_an_empty_queue_is_none(tmp_path):
    assert JobStore(tmp_path / "hub.db").claim_next() is None


def test_claim_next_is_fifo(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf(str(i)) for i in range(3)], force=False)
    assert [store.claim_next().id for _ in range(3)] == res.created
    assert store.claim_next() is None


def test_claim_next_starts_exactly_one_job(tmp_path):
    """One statement, one row -- and the subquery is evaluated once, not per candidate.

    `WHERE id = (SELECT id FROM job WHERE status='queued' ... LIMIT 1)` is correct only
    because SQLite evaluates a subquery with no outer reference a single time. Were it
    re-evaluated per candidate row, the first row examined would become `running` and the
    next evaluation would name a different id -- so one call would claim the entire queue in
    a single sweep. The *count* is what catches that; the `a.id != b.id` assertion does not,
    because a one-at-a-time implementation returns a different id for the second call too.
    """
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf(str(i)) for i in range(5)], force=False)
    store.claim_next()
    assert len([job for job in store.list() if job.status == "running"]) == 1
    assert len([job for job in store.list() if job.status == "queued"]) == 4


def test_claim_next_stamps_started_at_and_leaves_finished_at_alone(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf()], force=False)
    job = store.claim_next()
    assert job.status == "running"
    assert job.finished_at is None
    # An offset, not a naive stamp: the browser renders these and a stamp with no
    # zone in it reads as local time in whatever timezone the reader is in.
    assert datetime.fromisoformat(job.started_at).tzinfo is not None
    assert datetime.fromisoformat(job.created_at).tzinfo is not None


def test_claim_next_is_exclusive_under_real_concurrency(tmp_path):
    """The exclusivity claim, proved the only way it can be.

    Twelve threads, twelve jobs, twelve stores -- one connection each, on one file, all
    released from a barrier at the same instant. Nothing in this test shares a lock with the
    code under test, so the only thing that can stop two threads claiming one job is the
    single-statement `UPDATE`. Against a read-then-write implementation two threads would
    read the same queued id and both write to it, and the count of distinct ids is what says
    so. The brief's version cannot: one connection, one thread, and any correct pair of
    consecutive claims differs.
    """
    path = tmp_path / "hub.db"
    jobs = 12
    JobStore(path).create_batch("u", "album", [leaf(str(i)) for i in range(jobs)], force=False)

    barrier = threading.Barrier(jobs)
    claimed: list[list[int]] = [[] for _ in range(jobs)]
    failures: list[BaseException] = []

    def claim(index: int) -> None:
        own = JobStore(path)
        try:
            barrier.wait(timeout=30)
            while True:
                job = own.claim_next()
                if job is None:
                    return
                claimed[index].append(job.id)
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            failures.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, f"a claiming thread raised: {failures[0]!r}"
    flat = [job_id for got in claimed for job_id in got]
    assert len(flat) == jobs, f"expected {jobs} claims in total, got {len(flat)}"
    assert sorted(flat) == sorted(set(flat)), f"an id was claimed twice: {sorted(flat)}"
    final = JobStore(path)
    assert len([job for job in final.list() if job.status == "running"]) == jobs


def test_claim_next_reports_an_empty_queue_instead_of_blocking(tmp_path):
    """Two threads, one job: the loser is told the queue is empty, it is not left waiting.

    A claim that blocked on the write lock would stall the scheduler loop instead of
    returning, and a caller polling a queue another task is draining would never finish.
    `busy_timeout` is what bounds the wait at 5 s; the answer after it is a `None`.
    """
    path = tmp_path / "hub.db"
    JobStore(path).create_batch("u", "album", [leaf()], force=False)
    results: list[object] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        own = JobStore(path)
        try:
            barrier.wait(timeout=30)
            results.append(own.claim_next())
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            failures.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, f"a claiming thread raised: {failures[0]!r}"
    assert len([r for r in results if r is None]) == 1
    assert len([r for r in results if r is not None]) == 1


def test_two_stores_on_one_file_see_each_others_rows(tmp_path):
    """Two connections, one file. This is also what makes WAL observable at all.

    `busy_timeout` and `foreign_keys` are per connection, so `__init__` sets them on every
    store; `journal_mode` is a property of the file, which is why a store opened by a second
    thread is still in WAL without anything being done for it.
    """
    path = tmp_path / "hub.db"
    writer = JobStore(path)
    reader = JobStore(path)
    writer.create_batch("u", "album", [leaf("1")], force=False)
    assert [job.adam_id for job in reader.list()] == ["1"]
    writer.close()
    assert [job.adam_id for job in reader.list()] == ["1"]
    reader.close()
    # Still there after every connection is closed: the state is in the file, not in a
    # process that is about to exit.
    assert [job.adam_id for job in JobStore(path).list()] == ["1"]


# --- marking ---------------------------------------------------------------


def test_get_returns_none_for_an_unknown_id(tmp_path):
    assert JobStore(tmp_path / "hub.db").get(4242) is None


def test_mark_records_progress_and_leaves_the_fields_it_was_not_given(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, "running", progress=0.25, bytes_done=256, bytes_total=1024)
    job = store.get(job_id)
    assert (job.progress, job.bytes_done, job.bytes_total) == (0.25, 256, 1024)
    # Progress is not derived from the status, because only the caller knows whether it is
    # pausing a transfer or restarting one. A store that reset it on every `mark` would
    # clear the bytes a paused job still legitimately reports.
    assert job.skip_reason is None and job.error is None


def test_mark_records_a_skip_reason_and_an_error(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, "skipped", skip_reason="already in 4 - Leaves")
    assert store.get(job_id).skip_reason == "already in 4 - Leaves"
    store.mark(job_id, "failed", error="wrapper said regions: []")
    assert store.get(job_id).error == "wrapper said regions: []"
    assert store.get(job_id).skip_reason == "already in 4 - Leaves"


@pytest.mark.parametrize("status", sorted(JOB_STATUSES))
def test_finished_at_is_set_exactly_for_the_terminal_statuses(tmp_path, status):
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, status)
    job = store.get(job_id)
    assert job.status == status
    assert (job.finished_at is not None) == (status in TERMINAL_STATUSES)


def test_re_queuing_a_finished_job_clears_its_finished_at(tmp_path):
    """`POST /api/jobs/{id}/retry` re-queues a row that already has a `finished_at`.

    Leaving it would render a queued job as finished. The invariant behind
    `test_finished_at_is_set_exactly_for_the_terminal_statuses` is what prevents that: the
    column is a function of the status and nothing else, so no caller has to remember to
    clear it, and no caller can get it wrong by marking a job `queued` from a script.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, "failed", error="nope")
    store.mark(job_id, "queued")
    job = store.get(job_id)
    assert job.finished_at is None
    assert store.claim_next().id == job_id


# --- B2: a finished job cannot become active again except by being re-queued ---- #
#
# The rule lives here rather than in the one caller that breaks it, because it is a property
# of the *table*: any status write that moves a terminal row into an active status produces a
# job that `DELETE` and `retry` both refuse ("not finished, so there is nothing to retry"),
# so nothing can ever release it.


@pytest.mark.parametrize("finished", sorted(TERMINAL_STATUSES))
@pytest.mark.parametrize("target", ("running", "waiting"))
def test_a_finished_job_cannot_be_marked_active_again(tmp_path, finished, target):
    """The transition, refused, from every terminal status to every active one.

    All twelve combinations, because the rule is a product of two closed sets and a version
    that only refused `done -> running` would pass a test that only checked that pair.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, finished)
    before = store.get(job_id)

    with pytest.raises(IllegalTransition) as excinfo:
        store.mark(job_id, target, progress=0.5, bytes_done=5, bytes_total=10)

    # The message has to name the row's real state, because the two refusals (no such job, and
    # forbidden transition) are both a zero-row `UPDATE` and a reader needs to tell them.
    assert repr(finished) in str(excinfo.value)
    assert repr(target) in str(excinfo.value)

    # And the row is completely untouched -- this is the part that matters, because the old
    # behaviour was not "wrong value written" but "row moved out of the terminal set".
    after = store.get(job_id)
    assert after.status == finished
    assert after.finished_at == before.finished_at, "a refused mark must not clear finished_at"
    assert after.progress is None
    assert after.bytes_done is None


def test_re_queuing_is_the_one_door_out_of_a_finished_job(tmp_path):
    """`queued` is legal from any status, so retry keeps working after a refusal.

    The complement of the test above, and the reason the rule carves out `queued` rather than
    refusing every active target: `POST /api/jobs/{id}/retry` marks `queued`, and it is the
    only thing that can revive a finished row on purpose.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, "done")
    with pytest.raises(IllegalTransition):
        store.mark(job_id, "running")
    store.mark(job_id, "queued")
    assert store.get(job_id).status == "queued"
    assert store.claim_next().id == job_id
    # And once claimed, `running` is reachable again -- the row is not bricked by the refusal.
    assert store.get(job_id).status == "running"


def test_an_active_job_can_still_be_marked_running(tmp_path):
    """The refusal must not catch the one legitimate `mark(..., "running")`.

    That call exists: `hub.app._apply_progress` writes progress on a `running` row on every
    0.1 s tick. A rule that read "cannot be marked running" rather than "cannot become
    active again" would break the progress bar entirely, and this is the test that says so.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.claim_next()
    for fraction, done in ((0.25, 25), (0.5, 50), (1.0, 100)):
        store.mark(job_id, "running", progress=fraction, bytes_done=done, bytes_total=100)
        job = store.get(job_id)
        assert job.status == "running"
        assert job.progress == fraction
        assert job.bytes_done == done
        assert job.finished_at is None


def test_an_illegal_transition_and_a_missing_job_are_different_errors(tmp_path):
    """Zero rows is ambiguous, and the two answers want different messages.

    `mark` on an id that was never there is a caller and the store disagreeing about the
    world -- a `LookupError`. `mark` that the table forbids is a race the caller is expected
    to handle -- a `RuntimeError`, and the base `RipperHostError` shares, so a caller with a
    broad `except RuntimeError` around a whole job catches it rather than letting it escape
    from a worker callback.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]

    with pytest.raises(JobNotFound):
        store.mark(9999, "queued")
    store.mark(job_id, "done")
    with pytest.raises(IllegalTransition):
        store.mark(job_id, "running")

    assert issubclass(IllegalTransition, RuntimeError)
    assert not issubclass(IllegalTransition, ValueError), (
        "a ValueError would say 'you passed something wrong', which is a bug to fix; this is "
        "the world moving on, which is a race to handle"
    )


def test_mark_rejects_an_unknown_status(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    with pytest.raises(ValueError, match="pause"):
        store.mark(job_id, "pause")
    assert store.get(job_id).status == "queued"


def test_mark_rejects_an_unknown_field_and_changes_nothing(tmp_path):
    """A misspelled `bytes` must not be a silent no-op.

    `mark(**fields)` has no signature to check against, so a typo would leave the job
    silently un-updated, and a progress bar that stops moving with no error anywhere is a
    bug that is only ever found by a user. The row is asserted unchanged, so the refusal
    happens before the write rather than instead of it.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    with pytest.raises(ValueError, match="bytes"):
        store.mark(job_id, "running", bytes=1024)
    assert store.get(job_id).status == "queued"


def test_marking_a_job_that_is_not_there_raises(tmp_path):
    with pytest.raises(JobNotFound):
        JobStore(tmp_path / "hub.db").mark(4242, "done")


# --- listing ----------------------------------------------------------------


def test_list_orders_by_id_and_filters_by_status(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    first = store.create_batch("u", "album", [leaf("1"), leaf("2")], force=False).created
    store.mark(first[0], "done")
    second = store.create_batch("v", "playlist", [leaf("3")], force=False).created
    assert [job.id for job in store.list()] == first + second
    assert [job.id for job in store.list(status="done")] == [first[0]]
    assert [job.id for job in store.list(status="queued")] == [first[1], second[0]]


def test_list_orders_by_id_and_not_by_created_at(tmp_path):
    """M5: the ordering claim, in the one case where the two orderings differ.

    `create_batch` deliberately shares a single `created_at` across every leaf of a batch --
    one `_now()` per call, not per leaf -- so within a batch `created_at` cannot order anything
    and `id` is the only tiebreak there is. Which means every test that enqueues a batch lands
    inside a single millisecond, and `ORDER BY created_at` agrees with `ORDER BY id` by
    accident. The rows are moved apart here so the two orderings disagree, and the assertion
    is that `id` still wins.
    """
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1"), leaf("2")], force=False)
    store.create_batch("v", "album", [leaf("3"), leaf("4")], force=False)
    earlier = sqlite3.connect(tmp_path / "hub.db")
    try:
        # Make jobs 1 and 2 look like they were enqueued *after* 3 and 4.
        earlier.execute(
            "UPDATE job SET created_at = '2099-01-01T00:00:00.000+00:00' WHERE id <= 2"
        )
        earlier.commit()
    finally:
        earlier.close()
    assert [job.id for job in store.list()] == [1, 2, 3, 4]
    # ...and the two orderings really are different, so the assertion above is not vacuous.
    assert [job.id for job in sorted(store.list(), key=lambda job: job.created_at)] == [3, 4, 1, 2]


def test_a_store_cannot_be_used_from_another_thread(tmp_path):
    """M6: `check_same_thread` is load-bearing, and it is the reason for a documented shape.

    A connection used from a foreign thread raises `ProgrammingError` on the first statement,
    so a store is per-thread rather than per-process. That is the shape the concurrency tests
    use (a store each), and it is the reason `JobStore`'s docstring says asyncio tasks on one
    thread may share one while threads may not. Asserted because the alternative -- turning the
    check off for convenience -- would put a non-thread-safe handle in reach of a caller who
    assumed it was safe, and SQLite would then serialise or corrupt rather than refuse.
    """
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1")], force=False)
    failures: list[BaseException] = []

    def use_it() -> None:
        try:
            store.list()
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            failures.append(exc)

    thread = threading.Thread(target=use_it)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], sqlite3.ProgrammingError), (
        f"expected a ProgrammingError from a foreign thread, got {failures!r}"
    )


def test_list_rejects_an_unknown_status(tmp_path):
    with pytest.raises(ValueError, match="finished"):
        JobStore(tmp_path / "hub.db").list(status="finished")


def test_list_has_no_top_level_filter_because_nothing_writes_a_parent_id(tmp_path):
    """`parent_id` is in the schema and in the brief's `Job`, and nothing sets it yet.

    `create_batch`'s signature -- the brief's -- has no `parent_id`
    parameter, so every row it writes is top level and `parent_id=None` has to mean "no
    filter" rather than "top level only": `GET /api/jobs?parent=` is absent-means-no-
    filter like every other query parameter. Asserted so the two readings cannot be swapped
    by a later task, and so the day a row does carry a parent the filter is already the one
    the API needs.

    **And there is no `parent_url` filter**, which is the point of the assertion: filtering a
    batch by `parent_id` is impossible today, so a caller that needs "the jobs of *this*
    request" has nothing to filter on and `list()` returning everything is the only answer it
    has. The API handler needs `list(parent_url=...)`; it is not here, on purpose -- adding a
    query parameter before the caller exists is how `parent_id` ended up in this signature in
    the first place.
    """
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1")], force=False)
    assert store.get(res.created[0]).parent_id is None
    assert [job.id for job in store.list(parent_id=None)] == res.created
    assert store.list(parent_id=res.created[0]) == []


# --- resuming a token-blocked queue ----------------------------------------


def test_resume_waiting_on_a_queue_with_nothing_waiting_is_zero(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1"), leaf("2")], force=False)
    assert store.resume_waiting() == 0
    assert len([job for job in store.list() if job.status == "queued"]) == 2


def test_a_resumed_job_is_claimed_again_rather_than_queued_behind_the_rest(tmp_path):
    """A token-blocked job keeps its place, because `id` is the only ordering there is.

    The queue is ordered by `id` and by nothing else, so a job that was parked goes back to
    the front of the line rather than the back. The alternative -- ordering by `created_at`,
    or by `started_at`, which `claim_next` has just overwritten -- would silently reorder what
    the user asked for, and the reordering would be invisible.
    """
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1"), leaf("2"), leaf("3")], force=False)
    blocked = store.claim_next()
    store.mark(blocked.id, "waiting")
    store.claim_next()
    store.resume_waiting()
    # Job 2 is `running` and job 3 is still `queued`, so these two claims are all that is
    # available -- and the first is the resumed job, not job 3. `created_at` is not the
    # ordering key and `started_at` has just been overwritten, so `id` can only be, and `id`
    # puts a resumed job back in front of the one that was queued behind it.
    assert [store.claim_next().id for _ in range(2)] == [blocked.id, res.created[2]]


def test_resume_waiting_keeps_the_reason_the_queue_stopped(tmp_path):
    """`error` survives a resume, unlike `finished_at`.

    Both are stale-on-rerun, and the two are treated differently on purpose. `finished_at`
    is a function of the status, so it is derived. "The Apple token expired" is not
    derivable and nothing else in the row carries it, so clearing it would erase the only
    record that the queue stalled for that reason. The next `claim_next` does clear it,
    because a job that has just started has no error yet -- the row describes the current
    run, and `started_at` beside it is already the new one.
    """
    store = JobStore(tmp_path / "hub.db")
    job_id = store.create_batch("u", "album", [leaf()], force=False).created[0]
    store.mark(job_id, "waiting", error="token expired")
    store.resume_waiting()
    assert store.get(job_id).error == "token expired"
    assert store.claim_next().error is None


# --- the broker -------------------------------------------------------------


def frame(payload: dict) -> str:
    """The one way a payload becomes a frame, so the tests do not restate it.

    Deliberately not the broker's own `frame()`: restating the contract means a change to the
    implementation fails the test that depends on it, rather than both moving together.
    """
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


# Long enough that a loaded machine does not cause a spurious failure, short enough that a
# hang is a failure in seconds rather than in the harness's own 180 s.
PARK_TIMEOUT = 5.0


async def next_chunk(agen, timeout: float = PARK_TIMEOUT) -> str:
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def read(agen, count: int) -> list[str]:
    return [await next_chunk(agen) for _ in range(count)]


async def parked(agen):
    """Start `agen` and leave it waiting on the live queue, then return the pending read.

    The single-yield of `__anext__` is wrapped in `wait_for` here for the same reason
    `next_chunk` does: without it, a broker that answered a quiet channel (or published
    nothing) would leave the test waiting on the harness's own 180 s timeout instead of
    failing with a message that says which invariant broke. Every read in this section goes
    through one of these two helpers, so no test here can hang.
    """
    task = asyncio.ensure_future(
        asyncio.wait_for(agen.__anext__(), PARK_TIMEOUT)
    )
    await asyncio.sleep(0)
    assert not task.done(), "the subscriber answered before anything was published"
    return task


async def unpark(task):
    """Cancel a parked read and let it settle, so `aclose()` is not called on a running
    generator. A client that disconnects mid-stream is exactly this: the request task is
    cancelled while the SSE generator sits in `await queue.get()`."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def settle(task):
    """Await a parked read that is expected to deliver, with the same timeout as `next_chunk`."""
    return await asyncio.wait_for(task, PARK_TIMEOUT)


async def test_a_live_message_reaches_a_subscriber_that_is_already_waiting():
    broker = EventBroker()
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    broker.publish("jobs", {"kind": "progress", "id": 1})
    assert await settle(task) == frame({"kind": "progress", "id": 1})
    await agen.aclose()


async def test_two_subscribers_both_receive_what_is_published_to_their_channel():
    broker = EventBroker()
    jobs, logs = broker.subscribe("jobs"), broker.subscribe("logs")
    first, second = await parked(jobs), await parked(logs)
    broker.publish("jobs", {"n": 1})
    broker.publish("logs", {"n": 2})
    assert (await settle(first), await settle(second)) == (
        frame({"n": 1}),
        frame({"n": 2}),
    )
    await jobs.aclose()
    await logs.aclose()


async def test_a_subscriber_finds_no_snapshot_on_a_channel_nobody_published_on():
    """An invented empty snapshot is worse than silence.

    A browser that connects to a quiet channel and is handed `{"kind": "snapshot", "jobs":
    []}` would render an empty queue the store never asserted -- a lie that looks like good
    news, and it would be indistinguishable from a real one. So nothing is buffered until
    something is published, and a subscriber waits. The snapshot is the API layer's to
    publish, from a real `list()`.
    """
    broker = EventBroker()
    agen = broker.subscribe("jobs")
    with pytest.raises(TimeoutError):
        await next_chunk(agen, 0.05)
    await agen.aclose()


async def test_a_message_published_after_the_backlog_and_before_the_live_wait_is_not_lost():
    """The window that a "drain, then subscribe" ordering drops.

    A subscriber takes its backlog, and the test publishes into the gap before it asks for
    the next message. If registration happened after the backlog was read, this message would
    have gone to nobody: the queue would be one update short with no error anywhere, and the
    UI would sit on a stale queue until the next unrelated event. Registering and reading the
    backlog are therefore one synchronous step, with no `await` between them for the event
    loop to interleave into.
    """
    broker = EventBroker()
    broker.publish("jobs", {"n": 0})
    agen = broker.subscribe("jobs")
    assert await next_chunk(agen) == frame({"n": 0})
    broker.publish("jobs", {"n": 1})
    assert await next_chunk(agen) == frame({"n": 1})
    await agen.aclose()


async def test_the_backlog_is_the_last_fifty_messages_and_no_more():
    broker = EventBroker()
    for n in range(HISTORY + 10):
        broker.publish("jobs", {"n": n})
    agen = broker.subscribe("jobs")
    try:
        got = await read(agen, HISTORY)
        assert [json.loads(chunk[6:])["n"] for chunk in got] == list(range(10, HISTORY + 10))
        with pytest.raises(TimeoutError):
            await next_chunk(agen, 0.05)
    finally:
        await agen.aclose()


async def test_a_late_subscriber_is_not_pitched_another_channel_s_backlog():
    broker = EventBroker()
    broker.publish("logs", {"line": "hello"})
    agen = broker.subscribe("jobs")
    with pytest.raises(TimeoutError):
        await next_chunk(agen, 0.05)
    await agen.aclose()


async def test_a_publish_after_a_subscriber_was_cancelled_still_reaches_the_next_one():
    """A subscriber that disconnects mid-stream costs nothing and breaks nothing.

    A browser tab closing is a cancelled request task, not a tidy `break`, so the `finally`
    has to run on cancellation and not only on `aclose()`. Both paths are counted below, and
    the broker is then shown still working.
    """
    broker = EventBroker()
    first = broker.subscribe("jobs")
    task = await parked(first)
    assert broker.subscriber_count("jobs") == 1
    await unpark(task)
    assert broker.subscriber_count("jobs") == 0, "cancelling the read did not unregister"
    await first.aclose()
    broker.publish("jobs", {"n": 1})
    second = broker.subscribe("jobs")
    try:
        assert await next_chunk(second) == frame({"n": 1})
    finally:
        await second.aclose()


async def test_closing_a_subscription_unregisters_it():
    """I4: the anti-leak `finally`, pinned on a deterministic path.

    There is no way to see this from outside the broker -- a subscriber that stays registered
    costs memory and a little work per publish, and nothing anyone can look at says so. So the
    broker carries a read-only `subscriber_count` and the count is the assertion; without the
    count the only way to test the `finally` is to read `_channels`, and a method that admits
    it is a diagnostic is better than a test that reaches inside.

    Removing the `finally` fails this test and the two below it.
    """
    broker = EventBroker()
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    assert broker.subscriber_count("jobs") == 1
    assert broker.subscriber_count("nowhere") == 0
    # One message read, so the generator sits at a `yield` rather than inside `get()` -- which
    # is where a consumer that breaks out of `async for` leaves it, and the only state
    # `aclose()` can legally be called in.
    broker.publish("jobs", {"n": 1})
    assert await settle(task) == frame({"n": 1})
    assert broker.subscriber_count("jobs") == 1
    await agen.aclose()
    assert broker.subscriber_count("jobs") == 0, "aclose() did not unregister"
    # And the generator is finished, not merely unregistered.
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


async def test_two_subscribers_are_counted_separately():
    broker = EventBroker()
    one, two = broker.subscribe("jobs"), broker.subscribe("jobs")
    assert broker.subscriber_count("jobs") == 0, "subscribe() registered before the first read"
    first, second = await parked(one), await parked(two)
    assert broker.subscriber_count("jobs") == 2
    broker.publish("jobs", {"n": 1})
    assert await settle(first) == frame({"n": 1})
    await one.aclose()
    assert broker.subscriber_count("jobs") == 1, "closing one closed the other"
    await settle(second)
    await two.aclose()
    assert broker.subscriber_count("jobs") == 0


async def test_a_subscriber_that_falls_too_far_behind_is_told_rather_than_fed():
    """I3: the live queue is bounded, and the overflow is a signal, not a silent drop.

    Unbounded, a browser tab that stopped reading grew its queue for the life of the process --
    measured at 200,000 retained frames, about 8 MB, for one subscriber. What the broker does
    *not* do is choose the policy: it counts what the subscriber missed and raises
    `SubscriberOverrun` on its next turn, so the API layer's SSE handler decides whether its
    client reconnects, re-reads a snapshot, or renders "connection lost". The broker cannot
    know
    whether the stream is a queue state (where a lost message costs nothing) or a log line
    (where it is the whole point), and it will not quietly deliver the stale frames on the
    way out -- `depth` is 7 because 3 fit in a queue of 3 and 7 did not.
    """
    broker = EventBroker(queue_size=3)
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    for n in range(10):
        broker.publish("jobs", {"n": n})
    with pytest.raises(SubscriberOverrun) as caught:
        await settle(task)
    assert (caught.value.channel, caught.value.depth, caught.value.limit) == ("jobs", 7, 3)
    # Unregistered on the way out, so the overrun does not leave a reader nothing reads.
    assert broker.subscriber_count("jobs") == 0


async def test_publishing_to_a_full_subscriber_does_not_raise_and_does_not_grow():
    """`publish` is on the scheduler's path, so a slow browser must never break it.

    Twenty messages to a queue of three: the first three are enqueued, the other seventeen are
    counted, and the publisher sees none of them. The alternative -- `put_nowait` on an
    unbounded queue -- could not raise, which is exactly why the growth was invisible.
    """
    broker = EventBroker(queue_size=3)
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    for n in range(20):
        broker.publish("jobs", {"n": n})
    with pytest.raises(SubscriberOverrun) as caught:
        await settle(task)
    assert caught.value.depth == 17
    # The history is still whole: the replay buffer is bounded by HISTORY, not by the queue,
    # so every published frame is still there for a subscriber that connects afterwards.
    published = 20
    replay = broker.subscribe("jobs")
    try:
        replayed = await read(replay, published)
        assert [json.loads(chunk[len("data: ") :])["n"] for chunk in replayed] == list(
            range(published)
        )
    finally:
        await replay.aclose()


async def test_a_subscriber_within_its_queue_is_never_told_it_overran():
    """The bound must not fire on a healthy reader, or the signal is worthless.

    `queue_size` is a capacity, not a policy: a subscriber that is keeping up must be
    indistinguishable from one on an unbounded queue, so the tests above do not become a
    reason to treat every stream as fragile.
    """
    broker = EventBroker(queue_size=3)
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    for n in range(3):
        broker.publish("jobs", {"n": n})
    delivered = await settle(task)
    assert json.loads(delivered[len("data: ") :]) == {"n": 0}
    assert broker.subscriber_count("jobs") == 1
    await agen.aclose()


async def test_the_default_queue_takes_a_whole_burst_without_complaint():
    """The default capacity is a capacity, and it is large enough to be invisible.

    `EventBroker()` with no arguments is what the SSE handler will write, so the default is
    what has to be right: a subscriber that is keeping up must never see
    `SubscriberOverrun`, or the signal becomes noise and the handler learns to ignore it. A
    full default queue's worth of messages --
    `SUBSCRIBER_QUEUE_SIZE`, read rather than restated -- is delivered one at a time with the
    reader interleaving, which is what a browser doing ordinary work looks like.
    """
    broker = EventBroker()
    agen = broker.subscribe("jobs")
    task = await parked(agen)
    for n in range(SUBSCRIBER_QUEUE_SIZE):
        broker.publish("jobs", {"n": n})
    first = await settle(task)
    got = await read(agen, SUBSCRIBER_QUEUE_SIZE - 1)
    assert [json.loads(chunk[len("data: ") :])["n"] for chunk in [first, *got]] == list(
        range(SUBSCRIBER_QUEUE_SIZE)
    )
    assert broker.subscriber_count("jobs") == 1
    await agen.aclose()


async def test_a_subscriber_that_is_never_started_receives_nothing():
    """M7: registering on `subscribe()` rather than on the first read would leak by itself.

    An unstarted generator holds its subscription open until it is garbage collected, so a
    browser tab that connects and is dropped by the server before the response is written
    would leave one behind. The generator is **held in a variable** here: bound to nothing it
    would be collected on the spot, and a broker that registered eagerly would pass this test
    for the wrong reason. The count is what makes the assertion real, and the second part --
    that a real subscriber still gets the message -- is what shows the channel is not broken.
    """
    broker = EventBroker()
    unstarted = broker.subscribe("jobs")
    assert unstarted is not None
    assert broker.subscriber_count("jobs") == 0, "subscribe() registered before the first read"
    broker.publish("jobs", {"n": 1})
    agen = broker.subscribe("jobs")
    try:
        assert await next_chunk(agen) == frame({"n": 1})
    finally:
        await agen.aclose()
    # Still unstarted and still not registered, even after the channel has been used.
    assert broker.subscriber_count("jobs") == 0


async def test_a_frame_is_one_sse_data_field_whatever_the_payload_contains():
    """SSE framing is the one thing here that fails silently if it is wrong.

    A payload carrying a newline, a carriage return, or a leading `data:` would break the
    stream if it were interpolated raw: the client would see two fields, and the second one
    would be dropped or shown as a separate message. `json.dumps` escapes all three inside
    strings, so a frame is always a single `data:` line closed by exactly one blank line, and
    this holds the payloads that would break it -- including a real Japanese album name, which
    must not be escaped into `\\uXXXX` either.
    """
    broker = EventBroker()
    payload = {
        "line": "a\nb\r\nc",
        "data": "data: not a field",
        "album": "薄塩指数",
        "empty": "",
        "nested": {"n": [1, 2]},
    }
    broker.publish("logs", payload)
    agen = broker.subscribe("logs")
    try:
        chunk = await next_chunk(agen)
    finally:
        await agen.aclose()
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    assert chunk.count("\n\n") == 1
    assert chunk[:-2].count("\n") == 0
    assert json.loads(chunk[len("data: ") : -2]) == payload
    assert "薄塩指数" in chunk


def test_a_payload_that_is_not_json_fails_at_publish_time():
    """At the publish, not at the read, and not swallowed.

    A message that cannot be serialised is a bug in the publisher -- a `Path` or a `Job` that
    was not converted. Surfacing it there names the caller; raising it inside the subscriber
    would instead look like a broken browser connection and would take the SSE stream down
    with it.
    """
    broker = EventBroker()
    with pytest.raises(TypeError):
        broker.publish("jobs", {"when": object()})


# --- what the SSE layer will serialise -------------------------------------


def test_every_public_object_survives_a_json_round_trip(tmp_path):
    """The SSE layer puts these straight into an SSE payload and into a JSON response.

    `asdict` is the call it will make, and `force` is the field most likely to break it: a
    `bool` in Python and an INTEGER in the row, where a `1` in place of a `true` is a silent
    type change rather than a crash.
    """
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf()], force=True)
    payload = {
        "job": asdict(store.get(res.created[0])),
        "batch": asdict(res),
        "leaf": asdict(leaf()),
    }
    assert json.loads(json.dumps(payload)) == payload
    assert payload["job"]["force"] is True
    assert payload["job"]["parent_url"] == "u" and payload["job"]["parent_type"] == "album"
    assert payload["batch"] == {"created": res.created, "skipped": [], "deduplicated": []}


# ---------------------------------------------------------------------------
# Clearing the queue.
#
# Two operations, and the asymmetry between them is the point. `delete_finished`
# is the first statement in this project that removes a row, so it is irreversible and
# it has to say so. `requeue` moves a row back to `queued` and is reversible, but it
# runs into `job_active_dedupe`: a row whose `(adam_id, codec)` is already held by an
# active job cannot go back, and a caller that does not report that is telling the user
# a track is queued when no row holds it.
# ---------------------------------------------------------------------------


def _seed_one_per_status(store: JobStore) -> dict[str, int]:
    """A row in every status, one `adam_id` each, and the ids keyed by status.

    One per status rather than several, so a test can say exactly which row survived.
    """
    ids: dict[str, int] = {}
    for n, status in enumerate(sorted(ACTIVE_STATUSES | TERMINAL_STATUSES)):
        batch = store.create_batch("u", "album", [leaf(str(100 + n))], force=False)
        ids[status] = batch.created[0]
        store.mark(ids[status], status)  # type: ignore[arg-type]
    return ids


def test_delete_finished_removes_terminal_rows_and_keeps_active_ones(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    ids = _seed_one_per_status(store)

    removed = store.delete_finished()

    # The ids, not a count: the caller has to forget each deleted job's leaf in the
    # in-memory registry, and `LeafRegistry` has no bulk clear -- so a count would leak
    # one entry per deleted row for the life of the process.
    assert sorted(removed) == sorted(ids[s] for s in TERMINAL_STATUSES)
    left = {job.id for job in store.list()}
    assert left == {ids[s] for s in ACTIVE_STATUSES}
    for status in TERMINAL_STATUSES:
        assert store.get(ids[status]) is None, f"{status} survived the delete"


def test_delete_finished_on_an_empty_queue_is_a_no_op(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    assert store.delete_finished() == []
    assert store.delete_finished() == []  # idempotent, because a UI may double-submit


def test_requeue_moves_failed_rows_back_to_queued_and_clears_the_outcome(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    ids = _seed_one_per_status(store)

    result = store.requeue({"failed"})

    assert result.requeued == [ids["failed"]]
    revived = store.get(ids["failed"])
    assert revived is not None and revived.status == "queued"
    # A row back on the queue must not still be carrying the previous outcome, or the
    # queue page renders the old failure next to a row that is about to run again.
    assert revived.error is None
    assert revived.skip_reason is None
    assert revived.started_at is None
    assert revived.finished_at is None
    assert revived.progress is None
    assert revived.bytes_done is None
    assert revived.bytes_total is None


def test_requeue_leaves_rows_in_the_other_statuses_alone(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    ids = _seed_one_per_status(store)

    store.requeue({"failed", "cancelled"})

    for status in JOB_STATUSES - {"failed", "cancelled"}:
        job = store.get(ids[status])
        assert job is not None and job.status == status, f"{status} was moved"


def test_requeue_reports_the_rows_the_dedupe_index_refuses(tmp_path):
    """A key another job already holds cannot go back, and the caller is told which.

    `create_batch` refuses a second row for a held key, so the refused row has to be
    made by hand -- which is what a real queue looks like after a schema change or a
    restored backup. Silently skipping it would leave the user watching a queue that
    does not contain what they asked for.
    """
    store = JobStore(tmp_path / "hub.db")
    # The holder: same key, still active, so it owns the dedupe slot.
    holder = store.create_batch("u", "album", [leaf("1")], force=False).created[0]
    # The candidate: a finished row for the same key. `create_batch` would have folded
    # it into the holder, so it is made by hand -- which is what a real queue looks like
    # after a schema change or a restored backup. A terminal row is outside the index's
    # predicate, so it is allowed to sit beside an active row for the same key.
    store._conn.execute(  # noqa: SLF001 - the schema is the point
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES ('u', 'album', '1', 't', 'alac', 'ja', 0, 'failed', 'now')"
    )
    candidate = store.list(status="failed")[-1].id
    assert candidate != holder

    result = store.requeue({"failed"})

    assert result.requeued == []
    assert result.refused == [candidate]
    assert store.get(candidate).status == "failed", "the refused row was moved anyway"


def test_requeue_never_moves_a_running_job(tmp_path):
    """A running row is not leftover, and moving it would start a second rip on it.

    "Everything except done" includes `running`, and that is the one status that must
    not be re-queued: the row is mid-download, `job_active_dedupe` holds its key, and
    `claim_next` would hand it to a second worker. The guard is here rather than in the
    API because a store method that can be made to double-rip a track is a footgun for
    the next caller as well.
    """
    store = JobStore(tmp_path / "hub.db")
    ids = _seed_one_per_status(store)

    # `running` is asked for *on purpose*. A first version of this test excluded it at
    # the call site, which proved only that requeue does not move a status you did not
    # ask for -- the store's guard was never executed, and deleting it left the suite
    # green. The invariant is that the store refuses it even when a caller offers it.
    result = store.requeue(JOB_STATUSES - {"done"})

    job = store.get(ids["running"])
    assert job is not None and job.status == "running"
    assert ids["running"] not in result.requeued
    assert ids["running"] not in result.refused


def test_requeue_does_not_count_a_row_that_is_already_queued(tmp_path):
    """A row already on the queue is not "requeued" -- nothing happened to it.

    "Everything except done" includes `queued`, and the compare-and-set succeeds against a
    row that is already `queued`, so a naive implementation reports it. The count then says
    "3 requeued" when only 2 moved, which is the number a user reads to decide whether the
    button did anything.
    """
    store = JobStore(tmp_path / "hub.db")
    ids = _seed_one_per_status(store)

    result = store.requeue({"queued", "failed"})

    assert result.requeued == [ids["failed"]]
    assert ids["queued"] not in result.requeued
    assert ids["queued"] not in result.refused
