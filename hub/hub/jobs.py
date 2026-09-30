"""The download queue: one table, one partial unique index, and one atomic claim.

**What is persisted here is `job` and nothing else.** The library on disk is the single
source of truth for "already downloaded", deliberately, because folders get moved,
renamed and deleted outside the app and a persistent index would go stale against them. So
this module never touches the filesystem, and there is no `recording` / `release` /
`library_file` table to add later: any such table would be that stale index, and
`dedup.find_duplicate` already answers the only question one would be asked.

**The dedup key is `(adam_id, codec)`, over active jobs only.**
`job_active_dedupe` is a *partial* unique index, and the three statuses in its predicate are
the whole of the queue's concurrency story:

- `language` and `force` are not in the key. Two requests for the same track in two
  languages are one download, and `force` means "re-download this even if it is on disk" --
  which the scheduler's per-file dedup check in `app.py` decides at execution time, not per
  queue entry. It is stored and honoured there, and it deliberately cannot buy a second
  concurrent job for one track.
- `waiting` is in the predicate on purpose: a job parked on an expired Apple token must not
  be re-run alongside a new one, and a *failed* or *cancelled* job must be re-runnable.
- `is_music_video` is not in the key either. The `job` table has no column for it; it
  selects the Widevine path and nothing else.

**The index is the only authority on whether a key is held.** `create_batch` therefore
attempts the insert and reads the resulting `IntegrityError`; it never SELECTs first. A
SELECT-then-INSERT would have to be correct about a concurrent writer as well, and the index
already is, for free and inside the database. The one thing this buys is an id: the holder's,
which the UI needs in order to link "already queued" to something.

That path only works because the connection is in **autocommit** (`isolation_level=None`).
Under Python's default isolation an INSERT failure leaves the implicit transaction open, and
every later statement on that connection fails with "cannot start a transaction within a
transaction" -- the dedup path would poison the store instead of serving it. Autocommit also
means a batch is not all-or-nothing, which is the right way round here: `create_batch` is
idempotent (the index makes a retry fold into what already landed), and a 19-track album must
not lose 19 tracks to one unusable `adam_id`.

**`claim_next` is a single statement, not a read and a write.**

    UPDATE job SET ... WHERE id = (SELECT id FROM job WHERE status='queued'
                                   ORDER BY id LIMIT 1) RETURNING *

`SELECT ... LIMIT 1` then `UPDATE ... WHERE id = that` is the queue pattern everyone writes
first, and it is wrong the moment there are two workers: both read the same queued id and both
write to it. The single-statement form is correct because the whole statement is one write
transaction, so a second `claim_next` blocks on the write lock (`busy_timeout`, 5 s) and then
re-evaluates the subquery against the state the first one committed.
`tests/test_jobs.py::test_claim_next_is_exclusive_under_real_concurrency` is the test that
tells the two apart; a sequential loop over one connection cannot, which is why a
sequential-loop version of that test is not enough on its own.

**Timestamps are the store's, not the caller's.** `started_at` belongs to `claim_next` alone
and `finished_at` is a function of the status: non-NULL exactly when the job is terminal.
That is what makes `POST /api/jobs/{id}/retry` land in a consistent row -- a re-queued
job has no `finished_at` -- without every caller having to remember to clear it. The four
payload columns (`progress`, `bytes_done`, `bytes_total`, `skip_reason`, `error`) are written
only when the caller passes them, because only the caller knows whether it is pausing a
transfer or restarting one.

`created_at` is UTC with an offset and millisecond precision. Milliseconds rather than
microseconds because that is exactly what the ECMAScript *Date Time String Format* specifies,
so `new Date(created_at)` in the browser parses it instead of relying on leniency.

**One name, two spellings.** The `job` table's columns are `url` / `url_type`; the
Python-facing `Job` and `create_batch` are `parent_url` / `parent_type`. Both are honoured
where each is the authority -- the columns are the ones `JOB_TABLE_SQL` creates, quoted
verbatim, and the attribute and parameter names are the ones the scheduler (`app.py`) and
the API layer (`api/jobs.py`) pass in. The mapping is the one function, `_job_from_row`.

**No migrations yet, and that is a decision rather than an omission.** There is one table, it
is created if absent, and a future column is an `ALTER TABLE` behind a `PRAGMA user_version`
check. Nothing here reads `user_version` yet because nothing has needed to.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Container, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

# The three vocabularies, as closed sets rather than a bare `Literal` a caller can walk
# past. `ACTIVE_STATUSES` is load-bearing -- it is the index's predicate, and the two are
# pinned to each other by a test in `tests/test_jobs.py` rather than by string interpolation,
# so a status added here without being added to the index cannot pass unnoticed.
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "waiting", "running"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "skipped", "cancelled"})
JOB_STATUSES: frozenset[str] = ACTIVE_STATUSES | TERMINAL_STATUSES

JobStatus = Literal["queued", "waiting", "running", "done", "failed", "skipped", "cancelled"]

# The five values `url_type`'s own comment in `JOB_TABLE_SQL` lists. A closed set because
# the API layer switches on it to decide between resolving one track and resolving an album,
# so a typo would store cleanly and be rendered as an unrecognised kind forever.
PARENT_TYPES: frozenset[str] = frozenset(
    {"song", "album", "artist", "playlist", "music-video"}
)

# The four things `mark` may write besides the status, in a fixed order so the SQL it builds
# is deterministic. `started_at` and `finished_at` are absent on purpose: the first is
# `claim_next`'s, the second is derived from the status, and a caller that could set either
# could break both invariants.
MARKABLE_FIELDS: tuple[str, ...] = (
    "progress",
    "bytes_done",
    "bytes_total",
    "skip_reason",
    "error",
)

# Quoted rather than generated, so the artifact a human reads in `sqlite_master` is the DDL
# as it was written down rather than something a loop reassembles. The one deviation is
# `IF NOT EXISTS`: opening a database that already holds a queue has to be a no-op, and
# this is the constructor for every connection.
JOB_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job (
  id           INTEGER PRIMARY KEY,
  url          TEXT    NOT NULL,
  url_type     TEXT    NOT NULL,   -- song|album|artist|playlist|music-video
  adam_id      TEXT,
  title        TEXT,               -- log display only; never compared
  codec        TEXT    NOT NULL,
  language     TEXT,
  force        INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL,   -- queued|waiting|running|done|failed|skipped|cancelled
  skip_reason  TEXT,
  parent_id    INTEGER REFERENCES job(id),
  progress     REAL,
  bytes_done   INTEGER,
  bytes_total  INTEGER,
  error        TEXT,
  created_at   TEXT    NOT NULL,
  started_at   TEXT,
  finished_at  TEXT
)
"""

# The predicate's three statuses are written out rather than interpolated from
# `ACTIVE_STATUSES`. What keeps the two in step is *behaviour*, not a string comparison:
# `test_every_active_status_holds_the_dedupe_slot` and
# `test_every_terminal_status_frees_the_dedupe_slot` in `tests/test_jobs.py` put each status
# on both sides of the boundary, so a status added to one place and not the other fails there.
# A schema check additionally parses the predicate back out of the file and
# compares it as a set, which is what catches a hand-edited `sqlite_master` after the fact.
DEDUPE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS job_active_dedupe
  ON job(adam_id, codec)
  WHERE status IN ('queued', 'waiting', 'running')
"""

DEDUPE_INDEX_NAME = "job_active_dedupe"

# How long a contended write waits before SQLite gives up, in milliseconds. The shape of
# the failure is that the caller sees `OperationalError: database is locked` rather than a
# silently lost write.
BUSY_TIMEOUT_MS = 5000


class JobStoreError(RuntimeError):
    """The database could not be opened or prepared.

    Separate from `JobNotFound` because the two need different answers: one is a deployment
    problem to fix before anything works, the other is a caller naming a row that is not
    there. Every message names the path, because "unable to open database file" on its own
    does not say which of a container's mounts is missing.
    """


class JobNotFound(LookupError):
    """A job id that is not in the queue.

    Raised rather than ignored: `mark` on a row that does not exist means the caller and the
    store disagree about the world, and a silent no-op would leave a job the UI is still
    showing as running with nothing to update it.
    """


class IllegalTransition(RuntimeError):
    """A status change the queue's own rules forbid -- a finished job made active again.

    **A distinct type, and not a `ValueError`, because the two mean opposite things to a
    caller.** `ValueError` is "you passed something wrong", which is a bug to fix at the call
    site. This is "the world moved on since you read it", which is a *race* the caller is
    expected to handle: the late progress reading in `hub.scheduler._apply_progress` catches it and
    drops the reading, which is the correct answer because the job it was describing has
    finished.

    So it is a `RuntimeError` -- a `RipperHostError` shares that base, and a caller that
    catches `RuntimeError` around a whole job gets this one too rather than an unhandled
    exception on a worker callback. What it must *not* be is silently ignored by `mark`,
    because the state it guards is a job with no way out.
    """


@dataclass(frozen=True, slots=True)
class Progress:
    """One reading of a transfer, as the seam reports it.

    `slots=True` because there is one of these per poll tick per running job and it is
    immutable; frozen because a progress reading that changed after it was handed over would
    be a reading of something else.

    `fraction` is `None` when the total is unknown, and that is not a degenerate case: the
    wrapper's HLS segments do not always carry a content length, so "bytes so far, size
    unknown" is a real state. `None` renders as an indeterminate bar, which is true; `0.0`
    renders as a bar that never moves, which reads as a hang.

    **Lives here, not in `hub.app`,** because `RipperHost` is the thing that can produce one
    and it is the only file allowed to reach into upstream's `Task`. Putting the shape in
    `jobs.py` -- with `Leaf` and `Job`, the two other things a job is made of -- is what
    lets `app.py` import it without importing the app.
    """

    bytes_done: int
    bytes_total: int | None
    fraction: float | None


@dataclass(frozen=True)
class Leaf:
    """One track, as `resolver.py` hands it over and `ripper_host.py` consumes it.

    Not `slots=True`, and that is load-bearing rather than an omission: several tests copy a
    leaf with `vars()`, and `slots` removes `__dict__` and makes that a TypeError in
    a test that reads like a typo. Frozen, because a leaf's `adam_id` and `codec` are half of
    the dedup key -- a leaf that could be edited after it was enqueued could be edited into a
    different key than the one the index was asked about.

    `is_music_video` selects the Widevine decryption path (`src/legacy/`) over FairPlay
    (`temari`) and is not persisted: the `job` table has no column for it.
    """

    adam_id: str
    title: str
    album_name: str
    artist_name: str
    codec: str
    language: str
    url: str
    storefront: str
    is_music_video: bool = False


@dataclass(frozen=True)
class Job:
    """One row of `job`, as the store reads it back.

    `parent_url` / `parent_type` are the Python-facing names for the `url` / `url_type`
    columns; see the module docstring. `parent_id` is the table's self-reference and is
    `None` for everything `create_batch` writes, because its signature has no `parent_id`
    parameter. It is here because the column is in the schema and `list(parent_id=...)`
    filters on it.

    `language` is a plain `str` although the column is nullable: `create_batch` is the only
    writer and it takes the language from a `Leaf`, which has no `None`. The column stays
    nullable because `JOB_TABLE_SQL` declares it without `NOT NULL`.
    """

    id: int
    parent_id: int | None
    parent_url: str
    parent_type: str
    adam_id: str | None
    title: str | None
    codec: str
    language: str
    force: bool
    status: JobStatus
    skip_reason: str | None
    progress: float | None
    bytes_done: int | None
    bytes_total: int | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class BatchResult:
    """What one `create_batch` did, per leaf.

    `skipped` is always empty here and that is the design, not an omission: the
    `POST /api/jobs` answer is `{created[], skipped[], deduplicated[]}`, and a track already
    on disk is discovered at **execution** time by the dedup check in the scheduler
    (`app.py`), not at enqueue time. A queued job can sit long enough for the file to be
    deleted underneath it, and a second filesystem check here would put a duplicate check
    with different timing into the codebase for the two to disagree about. The list is
    kept because the response shape is the route's, and it is the field the API layer
    fills from the execution-time result.
    """

    created: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    deduplicated: list[int] = field(default_factory=list)


def _now() -> str:
    """UTC, with an offset, to milliseconds. See the module docstring for the precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _is_dedupe_violation(exc: sqlite3.IntegrityError) -> bool:
    """Whether this error is `job_active_dedupe` refusing, and not some other constraint.

    SQLite reports a violated UNIQUE constraint by naming its **columns**, not its index:
    `UNIQUE constraint failed: job.adam_id, job.codec`. That is a positive identification of
    the rule, taken from the database rather than inferred from the absence of something.

    The inference it replaces -- "a holder exists, so it must be a duplicate" -- is unsound,
    and the case that breaks it is not exotic: any other integrity failure on a leaf whose key
    is already held arrives here with a holder present, and would be reported as
    "deduplicated", i.e. as a track the user can see queued that has no row at all. A trigger
    standing in for the failure is how
    `test_an_integrity_failure_that_is_not_the_dedup_index_is_raised` reaches it.

    Matching the message is a dependence on SQLite's wording, which is worth naming as the
    cost: it has been `UNIQUE constraint failed: <table>.<column>[, ...]` for the whole life of
    the format, and if it ever changes, this returns `False` for a real duplicate -- which
    turns into a raised `IntegrityError` at the call site, i.e. a loud failure rather than a
    silently dropped download.
    """
    message = str(exc)
    return "UNIQUE constraint failed" in message and all(
        f"job.{column}" in message for column in ("adam_id", "codec")
    )


@dataclass(frozen=True)
class RequeueResult:
    """What one `requeue` did, per row.

    `refused` is not an error list -- those rows are fine, they are simply held by a
    `job_active_dedupe` slot that another job occupies, so they cannot go back to
    `queued` while that job is active. **Reporting them is the whole point**: a caller
    that reported only `requeued` would show a queue that does not contain what the
    user asked for, and the missing rows would look like a bug in the queue rather than
    a duplicate that is already on its way.
    """

    requeued: list[int] = field(default_factory=list)
    refused: list[int] = field(default_factory=list)


def _job_from_row(row: sqlite3.Row) -> Job:
    """Map one row onto the dataclass, in the one place the two namings meet."""
    return Job(
        id=row["id"],
        parent_id=row["parent_id"],
        parent_url=row["url"],
        parent_type=row["url_type"],
        adam_id=row["adam_id"],
        title=row["title"],
        codec=row["codec"],
        language=row["language"],
        # `force` is a bool in Python and an INTEGER in the row. Anything that branches on it
        # in the browser is branching on `true` / `1`, which is a silent type change, so it
        # is converted here rather than left to `asdict`.
        force=bool(row["force"]),
        status=row["status"],
        skip_reason=row["skip_reason"],
        progress=row["progress"],
        bytes_done=row["bytes_done"],
        bytes_total=row["bytes_total"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class JobStore:
    """The queue: enqueue, claim, mark, read. One SQLite connection, one table.

    `db_path` is the same `Settings.db_path` `load_settings` resolves, on the
    hub-data volume. The connection is created eagerly and the schema is created on open, so
    a fresh deployment needs no migration step before its first request -- the failure it can
    have is not being able to open the file, and that is reported with the path in it.

    **One store per thread.** `sqlite3` connections are not shareable across threads and
    `check_same_thread` is left on, so a second thread opening its own store is the only
    supported shape -- and it is the shape the concurrency test uses, because a store shared
    between threads would serialise in the GIL and prove nothing. Asyncio tasks on one thread
    may share one store freely: these methods are synchronous and contain no `await`, so
    they cannot interleave with each other.

    **Not a singleton and not a cache.** Every read is a read of the file, and every answer
    is derived from the row that is there now, because the rule is that the filesystem is
    the truth and nothing in this process may hold a second opinion about it longer than one
    statement.
    """

    def __init__(self, db_path: Path) -> None:
        # `Path` is the annotation, but `str()` it rather than requiring a `Path`: a
        # caller wiring this to `settings.db_path` should not have to care, and sqlite3
        # accepts both.
        path = str(db_path)
        try:
            # `isolation_level=None` is autocommit, and it is what makes the dedup path
            # usable at all -- see the module docstring.
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            # `busy_timeout` and `foreign_keys` are per connection, so they are set here for
            # every store rather than once. `journal_mode` is a property of the *file*, so
            # the first connection is enough and the rest inherit it.
            self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute(JOB_TABLE_SQL)
            self._conn.execute(DEDUPE_INDEX_SQL)
        except sqlite3.Error as exc:
            raise JobStoreError(
                f"could not open the job database at {path}: {exc}. The parent directory has "
                f"to exist and be writable -- it belongs on the hub-data volume at "
                f"/data/hub.db, and a missing mount shows up here rather than as an empty "
                f"queue."
            ) from exc

    def close(self) -> None:
        """Close the connection. Idempotent, and safe to leave to the garbage collector.

        A store is held for the life of the process in normal use, so this exists for the
        places that do not: a test that reopens a file, and `app.py` shutting the hub down.
        """
        self._conn.close()

    # -- enqueueing ---------------------------------------------------------

    def create_batch(
        self,
        parent_url: str,
        parent_type: str,
        leaves: Sequence[Leaf],
        *,
        force: bool,
    ) -> BatchResult:
        """Enqueue every leaf, and report which were created and which folded into a job.

        The two outcomes are disjoint and exhaustive over the leaves: `created` holds the
        ids of rows this call inserted, and `deduplicated` holds the ids of the rows that
        already held each key. Nothing is ever silently dropped, and a leaf that arrived with
        no identity raises rather than being quietly not-created -- see
        `test_a_key_with_no_identity_in_it_is_refused`.

        A leaf whose key is already held is **not** an error and **not** a new row, whatever
        `force` says. The spec's index has no way to express `force`, which is the point: the
        user asking twice does not mean the track should download twice at the same time.

        Raises `ValueError` for an unusable `parent_type`, `parent_url` or leaf key -- and
        *only* for those, so that one `except ValueError` covers every way a caller can get
        this wrong. A `sqlite3.Error` that is not a dedup collision propagates instead
        (`IntegrityError` for a row the schema refuses for any other reason,
        `OperationalError` for a locked or closed database); it is never reported as
        `deduplicated`, because that would tell the user their track was queued when no row
        exists. `JobStoreError` is not raised here: it is a failure to *open* the database, and
        that happens in the constructor.

        A refused leaf does not roll the batch back -- the leaves before it are already rows --
        and the leaves after it are not attempted. The API layer is the one caller, so
        catching the `ValueError`, reading `list()` and telling the user what is queued is
        its job; a retry
        is safe because the index turns the leaves that did land into `deduplicated` entries
        rather than duplicates.
        """
        if parent_type not in PARENT_TYPES:
            raise ValueError(
                f"parent_type must be one of {sorted(PARENT_TYPES)}, got {parent_type!r}"
            )
        if not isinstance(parent_url, str) or not parent_url.strip():
            raise ValueError(
                f"parent_url is {parent_url!r}, which cannot identify the batch. Every job "
                f"carries it as the `url NOT NULL` column, and the API layer lists and "
                f"cancels by it, so a blank one would make the queue unroutable and "
                f"unlistable."
            )

        created: list[int] = []
        deduplicated: list[int] = []
        stamp = _now()
        for leaf in leaves:
            self._check_key(leaf)
            try:
                cursor = self._conn.execute(
                    "INSERT INTO job (url, url_type, adam_id, title, codec, language, force,"
                    " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (
                        parent_url,
                        parent_type,
                        leaf.adam_id,
                        leaf.title,
                        leaf.codec,
                        leaf.language,
                        int(force),
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Two independent reasons to re-raise, and both are load-bearing.
                #
                # First: is this the dedup index at all? SQLite names the *columns* of the
                # constraint it refused, so a NOT NULL or CHECK failure on a row the schema
                # rejects for some other reason lands here too. Inferring "so it must be a
                # duplicate" from "a holder exists" is not sound: a different failure on a
                # leaf whose key *is* held would then be reported as deduplicated, and the
                # user would be told their track was queued when no row was written. Reading
                # the error identifies the rule positively instead of by inference.
                #
                # Second: a holder that cannot be found means the index's own guarantee was
                # not what fired, so re-raise rather than invent an id.
                if not _is_dedupe_violation(exc):
                    raise
                holder = self._holder(leaf)
                if holder is None:
                    raise
                deduplicated.append(holder)
            else:
                created.append(cursor.lastrowid)
        return BatchResult(created=created, deduplicated=deduplicated)

    def _check_key(self, leaf: Leaf) -> None:
        """Refuse a leaf whose dedup key would mean nothing.

        `""` would collide with every other `""` -- across albums, codecs and URLs -- so
        unrelated tracks would fold into each other. `None` is the quieter failure: SQLite
        treats every NULL as distinct in a unique index, so a NULL `adam_id` neither
        deduplicates against anything nor prevents a second NULL row, and the index looks
        present while silently not applying. The column is nullable, so this is the only
        place the hole is closed.
        """
        for field_name in ("adam_id", "codec"):
            value = getattr(leaf, field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(
                    f"leaf {field_name} is {value!r}, which cannot be part of a dedup key: "
                    f"an empty value would collide with every other empty value, and a NULL "
                    f"never collides at all in a SQLite unique index. The track is enqueued "
                    f"by nothing here and skipped by nothing, which is worse than either."
                )

    def _holder(self, leaf: Leaf) -> int | None:
        """The id of the active job holding this leaf's key, or `None` if there is none.

        Runs only after the index has already refused an insert, so at most one row can match
        -- that is the index's own guarantee, and this query cannot return two.

        The status filter repeats the index's own predicate and is **not** redundant with it.
        The index constrains what may *exist*; it says nothing about which of the several rows
        that share a key over time this one should return, and `fetchone()` takes them in id
        order. With id 1 `done` and id 2 `queued` for one key, an unfiltered lookup returns
        **1** -- the finished job -- and the UI would then render "already queued as #1" for a
        job that will never run again. `test_deduplicated_names_the_running_job_not_the_oldest_one`
        is that case.
        """
        row = self._conn.execute(
            "SELECT id FROM job WHERE adam_id = ? AND codec = ?"
            " AND status IN ('queued', 'waiting', 'running')",
            (leaf.adam_id, leaf.codec),
        ).fetchone()
        return None if row is None else row["id"]

    # -- the scheduler ------------------------------------------------------

    def claim_next(self, exclude: Container[int] = ()) -> Job | None:
        """Take the oldest queued job and mark it running, or `None` if there is none.

        One statement, so it is atomic: a second caller blocks on the write lock for up to
        `busy_timeout` and then re-evaluates the subquery against what the first one
        committed, which is why twelve threads get twelve different jobs. See the module
        docstring for why the read-then-write version of this is not equivalent.

        `error` is cleared here rather than in `mark`. A job that has just started has no
        error yet, and leaving the previous run's behind would put a failure message next to
        a `started_at` that is not that run's -- the row describes the current run, and
        `resume_waiting` deliberately keeps the reason a job was parked until it runs again.

        `None` rather than a wait: a claim that blocked would stall the scheduler loop, so a
        caller polling a queue another task is draining is told the queue is empty and can go
        round again.

        `exclude` is for a pool: ids the caller has already looked at and released on this
        pass, so a job it declined to run is not handed back to it -- or to a sibling -- a
        moment later. Putting a row back and excluding it are both "leave it for later", and
        only one of them terminates. See the `skip` branch below.
        """
        skip = tuple(exclude)
        if skip:
            # `exclude` rather than "claim it and put it back", because putting it back
            # re-queues the row the very next statement will hand out again, and a caller
            # looping over claims has no way to tell that apart from progress. Both are
            # synchronous, so a loop that keeps re-claiming never reaches an `await` and the
            # whole event loop stops -- the WebSocket stream, every request and `docker stop`'s
            # grace period with them. Excluding makes the loop's next claim return the *next*
            # eligible row, or `None`, which is the answer a loop can act on.
            #
            # The placeholders are bound, never interpolated, and the set is a handful of ids
            # at most: a collision needs two active jobs for one `adam_id`, and there are two
            # codecs, so a row can be excluded at most once per claim.
            holes = ",".join("?" * len(skip))
            rows = self._conn.execute(
                f"UPDATE job SET status = 'running', started_at = ?, error = NULL"  # noqa: S608
                f" WHERE id = (SELECT id FROM job WHERE status = 'queued'"
                f" AND id NOT IN ({holes}) ORDER BY id LIMIT 1)"
                f" RETURNING *",
                (_now(), *skip),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "UPDATE job SET status = 'running', started_at = ?, error = NULL"
                " WHERE id = (SELECT id FROM job WHERE status = 'queued' ORDER BY id LIMIT 1)"
                " RETURNING *",
                (_now(),),
            ).fetchall()
        # A `running` row written here always carries a `started_at`, and nothing else in the
        # store can put a row into that state: the column is only ever written here, and
        # `mark` rejects it as a field. A `running` row with a null start time therefore means
        # a hand-written INSERT, which is what lets the API layer render `started_at` without
        # a null branch; a 16-thread concurrent claim run is what established that.
        return _job_from_row(rows[0]) if rows else None

    def mark(self, job_id: int, status: JobStatus, **fields) -> None:
        """Set a job's status, and any of `progress`, `bytes_done`, `bytes_total`,
        `skip_reason`, `error` passed alongside it.

        `finished_at` is not a field: it is set when `status` is terminal and cleared when it
        is not, so that "finished" is always a function of the status and never something a
        caller can leave stale by marking a failed job `queued` for a retry. The five fields
        are written only when passed, because only the caller knows whether it is pausing a
        transfer or restarting one.

        Both the status and the field names are checked against closed sets *before* the
        write, so a misspelled `bytes` is an error at the call site rather than a job whose
        progress silently stopped updating. Raises `JobNotFound` for an id that is not there.
        """
        if status not in JOB_STATUSES:
            raise ValueError(
                f"unknown job status {status!r}; expected one of {sorted(JOB_STATUSES)}"
            )
        unknown = set(fields) - set(MARKABLE_FIELDS)
        if unknown:
            raise ValueError(
                f"cannot set {sorted(unknown)} on a job; mark() takes "
                f"{list(MARKABLE_FIELDS)} and nothing else. A misspelled field would be a "
                f"silent no-op, and a progress bar that stops moving with no error anywhere "
                f"is only ever found by a user."
            )
        if status in ACTIVE_STATUSES and status != "queued":
            # **A finished job cannot become active again except by being re-queued**, which
            # is what a retry does. Any other terminal -> active transition is a caller
            # working from a stale copy of the row, and accepting it is not a small wrong
            # answer: it moves the row out of the terminal set, so `delete` and `retry` both
            # start refusing it with a 409 ("not finished, so there is nothing to retry") and
            # nothing will ever release it. The job is then stuck *displaying as running* with
            # no user action able to clear it.
            #
            # The case that actually happens is the late progress reading: the seam's sampler
            # is cancelled in a `finally`, which stops new readings but not one already handed
            # to the event loop, and that callback arrives after the job was marked `done`.
            #
            # **The predicate is in the `WHERE` clause rather than in a read-then-write.** A
            # `SELECT status` first would be a time-of-check-to-time-of-use gap: the only
            # caller that can lose that race is `_apply_progress`, which runs on a
            # `call_soon_threadsafe` callback interleaved with everything else on the loop,
            # and the whole point is that the check and the write must not be separable. The
            # guard costs one indexed comparison on the write that was happening anyway.
            #
            # `claim_next` is deliberately *not* subject to this: it is the only legitimate
            # way into `running`, it only ever moves a `queued` row, and it does its own
            # `UPDATE` rather than going through `mark`.
            illegal_transition = True
        else:
            illegal_transition = False

        assignments = ["status = ?", "finished_at = ?"]
        params: list = [status, _now() if status in TERMINAL_STATUSES else None]
        # Fixed order, so the SQL is the same statement for the same call.
        for name in MARKABLE_FIELDS:
            if name in fields:
                assignments.append(f"{name} = ?")
                params.append(fields[name])
        params.append(job_id)
        where = "id = ?"
        if illegal_transition:
            # See the comment above: the guard lives here, in the same statement as the write.
            placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
            where += f" AND status NOT IN ({placeholders})"
            params.extend(sorted(TERMINAL_STATUSES))
        cursor = self._conn.execute(
            f"UPDATE job SET {', '.join(assignments)} WHERE {where}", params
        )
        if cursor.rowcount == 0:
            # Zero rows is ambiguous between "there is no such job" and "the transition is
            # forbidden", and the two want very different messages. The read happens only on
            # this path, which is never the hot one.
            row = self._conn.execute(
                "SELECT status FROM job WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"no job with id {job_id} to mark {status!r}")
            raise IllegalTransition(
                f"job {job_id} is {row[0]!r}, which is finished, and cannot be marked "
                f"{status!r}. A job becomes active again only by being re-queued, which "
                f"mark({job_id}, 'queued') does and which is what POST /api/jobs/{job_id}"
                f"/retry calls. If you are holding a progress reading for it, the job "
                f"finished after the reading was taken -- drop the reading rather than "
                f"reviving the row."
            )

    def resume_waiting(self) -> int:
        """Move every `waiting` job back to `queued` and return how many moved.

        Called after a successful login: a token expiry parks running jobs in
        `waiting` rather than failing them. The jobs keep their place in the queue, because
        the queue is ordered by `id` and nothing else -- ordering by `created_at` or by
        `started_at`, which a claim has just overwritten, would silently reorder what the user
        asked for. `error` is left alone; see `claim_next` for why, and
        `test_resume_waiting_keeps_the_reason_the_queue_stopped` for what is lost when the
        job does run again.
        """
        return self._conn.execute(
            "UPDATE job SET status = 'queued' WHERE status = 'waiting'"
        ).rowcount

    # -- reading ------------------------------------------------------------

    # -- clearing the queue --------------------------------------------------

    def delete_finished(self) -> list[int]:
        """Remove every row in a terminal status, and return the ids that went.

        **The first statement in this project that deletes a row**, and irreversible.
        What it costs is the *record* that a track was attempted -- the title, the error
        and the `skip_reason` evidence paths. It does not cost the file: the filesystem is
        the single source of truth for what is downloaded, and the
        execution-time dedup check reads the library, not this table. So deleting a
        `done` row cannot cause a re-download; it removes a line from a queue log.

        A `running` row is never touched, for the same reason `delete_job` refuses it:
        the transfer is in flight and upstream owns its partial file. `TERMINAL_STATUSES`
        excludes `running` by construction, so this needs no separate check -- and
        `test_every_terminal_status_frees_the_dedupe_slot` is what keeps that true.

        Idempotent, because a UI that double-submits must not be able to turn the second
        click into an error.

        **Returns the ids rather than a count** because the caller also holds each job's
        `Leaf` in memory -- `LeafRegistry` has no bulk clear, so only the ids let it forget
        what is no longer queued. A count would leak one entry per deleted row.
        """
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        params = sorted(TERMINAL_STATUSES)
        # Read before write, in one transaction, so the ids cannot drift from the rows
        # deleted: another writer changing a status between the two statements would
        # otherwise produce a list the caller is then told to forget leaves for.
        with self._conn:  # type: ignore[attr-defined]
            doomed = [
                row["id"]
                for row in self._conn.execute(
                    f"SELECT id FROM job WHERE status IN ({placeholders})", params
                ).fetchall()
            ]
            self._conn.execute(f"DELETE FROM job WHERE status IN ({placeholders})", params)
        return sorted(doomed)

    def requeue(self, statuses: Collection[str]) -> RequeueResult:
        """Put rows in `statuses` back on the queue, and report the ones that could not.

        `statuses` is a collection rather than a single status because "re-queue what
        failed" and "re-queue everything that is not done" are both things a user wants,
        and they are the same operation over a different set.

        Two guards, and neither is a preference:

        - **A `running` row is never moved.** It is mid-transfer, `job_active_dedupe`
          holds its key, and `claim_next` would hand the same row to a second worker.
          Callers offering "everything except done" get this exclusion for free.
        - **A row whose key another job already holds is refused, not raised.** The
          partial unique index is doing its job; `_is_dedupe_violation` says so, and the
          row goes in `refused` so the caller can tell the user which ones are already
          on their way.

        Each row is a compare-and-set on its *current* status, so a row that moved
        between the read and the write is not clobbered -- the same reason `mark` is
        careful, and the reason the outcome columns are cleared rather than the row
        being rebuilt.
        """
        # `running` is excluded because moving it would double-rip a track; `queued` is
        # excluded because a row already there is not "requeued" -- nothing happens to it,
        # and counting it would inflate the number a user reads to see whether the button
        # did anything.
        wanted = sorted(set(statuses) - {"running", "queued"})
        if not wanted:
            return RequeueResult()
        placeholders = ", ".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT id, status FROM job WHERE status IN ({placeholders})", wanted
        ).fetchall()

        requeued: list[int] = []
        refused: list[int] = []
        for row in rows:
            try:
                cursor = self._conn.execute(
                    "UPDATE job SET status = 'queued', error = NULL, skip_reason = NULL,"
                    " started_at = NULL, finished_at = NULL, progress = NULL,"
                    " bytes_done = NULL, bytes_total = NULL"
                    " WHERE id = ? AND status = ?",
                    (row["id"], row["status"]),
                )
            except sqlite3.IntegrityError as exc:
                if not _is_dedupe_violation(exc):
                    raise
                refused.append(row["id"])
                continue
            (requeued if cursor.rowcount else refused).append(row["id"])
        return RequeueResult(requeued=sorted(requeued), refused=sorted(refused))

    def get(self, job_id: int) -> Job | None:
        """One job by id, or `None`. `None` rather than raising, because `GET /api/jobs/{id}`
        for an id the caller made up is a 404 and not a bug in the caller."""
        row = self._conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job_from_row(row)

    def get_many(self, job_ids: Sequence[int]) -> list[Job]:
        """Fetch selected rows for browser hydration, never the entire job table."""
        ids = tuple(dict.fromkeys(job_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT * FROM job WHERE id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        return [_job_from_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Count all statuses in SQL so summaries do not materialize the queue."""
        counts = {
            row["status"]: row["n"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM job GROUP BY status"
            ).fetchall()
        }
        counts["total"] = sum(counts.values())
        return counts

    def queue_window(self, terminal_limit: int = 100) -> dict:
        """All active rows plus only the newest `terminal_limit` terminal rows.

        Active work is never hidden behind history pagination. Finished rows use a stable id
        cursor and the returned page is ordered oldest-first like the main queue table.
        """
        if not 1 <= terminal_limit <= 1000:
            raise ValueError("terminal_limit must be between 1 and 1000")
        active_rows = self._conn.execute(
            "SELECT * FROM job WHERE status IN ('queued','waiting','running') ORDER BY id"
        ).fetchall()
        terminal_rows = self._conn.execute(
            "SELECT * FROM job WHERE status IN ('done','failed','skipped','cancelled') "
            "ORDER BY id DESC LIMIT ?", (terminal_limit + 1,)
        ).fetchall()
        has_more = len(terminal_rows) > terminal_limit
        recent = terminal_rows[:terminal_limit]
        all_jobs = [_job_from_row(row) for row in active_rows]
        all_jobs.extend(_job_from_row(row) for row in reversed(recent))
        all_jobs.sort(key=lambda job: job.id)
        return {
            "jobs": all_jobs,
            "history_has_more": has_more,
            "history_before_id": min((row["id"] for row in recent), default=None),
        }

    def history_page(self, *, before_id: int | None = None, limit: int = 100) -> dict:
        """One older terminal-job page; active jobs are always served separately."""
        if not 1 <= limit <= 500:
            raise ValueError("history page limit must be between 1 and 500")
        where = "status IN ('done','failed','skipped','cancelled')"
        params: tuple[int, ...] = ()
        if before_id is not None:
            where += " AND id < ?"
            params = (before_id,)
        rows = self._conn.execute(
            f"SELECT * FROM job WHERE {where} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "jobs": [_job_from_row(row) for row in reversed(page)],
            "has_more": has_more,
            "before_id": min((row["id"] for row in page), default=None),
        }

    def list(
        self,
        *,
        status: JobStatus | None = None,
        parent_id: int | None = None,
        parent_url: str | None = None,
    ) -> list[Job]:
        """Every job, oldest id first, optionally filtered.

        `None` on an argument means "no filter", which is what `GET /api/jobs`
        `?status=&parent=` means for an absent query parameter -- so `parent_id=None` is *not*
        "top level only". Nothing distinguishes those two cases today, because
        `create_batch`'s signature has no `parent_id` parameter and so writes `None` to every
        row; a sentinel would be a second way of saying "none" for a case that cannot arise
        yet.

        `parent_url` exists for the caller that has to report a *partial* batch.
        `create_batch` applies the leaves it could and then raises `ValueError` on one it
        could not, and the handler is required to tell the user what actually landed. There
        is exactly one way to ask that question today, and it is not `parent_id`: the column
        nothing writes, whose `None` means "no filter" rather than "top level", so filtering
        a batch by it returns the user's **entire** queue and the response names every job
        ever queued as part of this request. The `url` column is what `create_batch` wrote
        for every row of a batch, so it is the batch's real identity.

        Ordered by `id` and not by `created_at`, so that two jobs enqueued inside one
        millisecond keep the order they were created in, and so that a resumed job goes back
        to the front of the line rather than the back.
        """
        clauses: list[str] = []
        params: list = []
        if status is not None:
            if status not in JOB_STATUSES:
                raise ValueError(
                    f"unknown job status {status!r}; expected one of {sorted(JOB_STATUSES)}"
                )
            clauses.append("status = ?")
            params.append(status)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            params.append(parent_id)
        if parent_url is not None:
            if not isinstance(parent_url, str) or not parent_url.strip():
                raise ValueError(
                    f"parent_url is {parent_url!r}, which cannot be a filter. It is the "
                    f"`job` table's `url` column, and `create_batch` refuses a blank one "
                    f"for the same reason: matching nothing is not the same as matching "
                    f"every job."
                )
            clauses.append("url = ?")
            params.append(parent_url)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM job{where} ORDER BY id", params).fetchall()
        return [_job_from_row(row) for row in rows]
