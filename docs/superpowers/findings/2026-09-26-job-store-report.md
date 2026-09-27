# Task 7 report — Job store, queue dedup, and the event broker

**Branch:** `feat/phase-1-foundation` · **Status:** DONE_WITH_CONCERNS · **Date:** 2026-09-27

Files: `hub/hub/jobs.py` (new), `hub/hub/events.py` (new), `hub/tests/test_jobs.py` (new),
`hub/spike/task7_schema_check.py` (new, the verification harness — the same role
`spike/task4_real_library_check.py` and `spike/task5_real_binary_check.py` play for tasks 4
and 5).

No dependency changes: `pyproject.toml` and `uv.lock` are untouched. The store is stdlib
`sqlite3` and the broker is stdlib `asyncio`.

---

## 0. A stale `.pyc` was making the suite red before I started

The first thing I ran was the baseline, and it came back **132 passed, 1 failed**:

```
FAILED tests/test_supervisor.py::test_a_ttl_longer_than_the_child_window_is_clamped
assert (1790445841.5869517 - 1790445541.587036) <= 60.0
```

That is Task 5's test, not mine, and the difference was exactly 300.0 s — the requested
`twofa_ttl`. The cause is not a bug in `wrapper_supervisor.py`. The **entire module was being
executed from a stale bytecode cache**: `hub/hub/__pycache__/wrapper_supervisor.cpython-313.pyc`
recorded the current source's mtime and size, so Python's timestamp check passed and the pyc
was used, but the bytecode it held was compiled from an earlier revision in which line 608 was
`window = self._twofa_ttl` rather than the clamp that is in the file now:

```
loaded, line 608:   608   L35:     LOAD_FAST                0 (self)
                                   LOAD_ATTR               58 (_twofa_ttl)
                                   STORE_FAST              11 (window)

freshly compiled:   608   L35:     LOAD_GLOBAL             59 (min + NULL)
```

A scan of every module against a fresh compile found **all 38 methods of
`WrapperSupervisor` stale and nothing else stale**. `inspect.getsource` reads the *file*, so
it showed the clamped source while the *code object* being run had no clamp — which is why
this reads as a supervisor bug to anyone who does not know.

`find hub -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +` (a
gitignored, regenerable build artifact) and the suite is **133 passed**, with no source
change. Anyone working in this workspace should clear the caches before believing a failure
in `wrapper_supervisor.py`, and the `_pyc` mtime/size trap is worth a line in the repo's
`AGENTS.md`.

**The suite is 203 passing now: 133 pre-existing + 70 new.**

---

## 1. What was implemented

`hub/hub/jobs.py` — `Leaf`, `JobStatus`, `Job`, `BatchResult`, `JobStore`, `JobStoreError`,
`JobNotFound`, the DDL constants, and the three status vocabularies. `hub/hub/events.py` —
`EventBroker` and `HISTORY`. Every name and signature the brief pins is present and unchanged:
`JobStore.__init__/create_batch/claim_next/mark/get/list/resume_waiting` and
`EventBroker.publish/subscribe`, with `Leaf`'s nine fields in the brief's order and in the
brief's spelling (Task 6 and Task 8 import them; the order and the names are pinned by a test
so a rename cannot pass silently).

**The only table is `job`.** No `recording`, no `release`, no `library_file`, no index table,
no memoized lookup, and nothing in either module touches the filesystem — the library on disk
stays the single source of truth for "already downloaded" (§7.1), so a second opinion held in
this process could only ever go stale against files the user moves outside the app.

**`DEDUPE_INDEX_SQL` is §6's DDL, quoted.** It is written out rather than interpolated from
`ACTIVE_STATUSES` so the artifact a human reads in `sqlite_master` is the spec's own
sentence. That is the one place in the module a constant is deliberately duplicated, so the
duplication is pinned: the harness parses the predicate back out of the file and compares it
as a set (`A.2` above), and a status added to one without the other cannot pass.

**The index is the only authority on whether a key is held.** `create_batch` attempts the
insert and reads the resulting `IntegrityError`, then looks up the holder's id. It never
SELECTs first, so there is no window in which a pre-check and the index can disagree. The
autocommit connection (`isolation_level=None`) is what makes that path usable: under Python's
default isolation a failed INSERT leaves the implicit transaction open and every later
statement on that connection dies with "cannot start a transaction within a transaction", so
the dedup path would poison the store instead of serving it.

**`claim_next` is one statement.** `UPDATE job SET status='running', started_at=?, error=NULL
WHERE id = (SELECT id FROM job WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *`. Also
verified directly: SQLite evaluates that uncorrelated subquery exactly once, so one call
claims one row — `test_claim_next_starts_exactly_one_job` claims one of five and counts
`running` rows, because were the subquery re-evaluated per candidate row a single call would
claim the whole queue in one sweep.

---

## 2. Decisions the brief left open

Every one of these was mine to make and every one is pinned by a test.

**1. The columns are §6's (`url`, `url_type`); the dataclass and the parameters are the
brief's (`parent_url`, `parent_type`).** This is the one place the brief and the spec name the
same thing differently. You told me the spec is the authority for the schema and the brief is
the authority for the fields Task 6/8 import, so both spellings are honoured where each is
authoritative: the DDL is §6's verbatim (a rename would have made the "schema is the spec's"
instruction false), `Job.parent_url` and `create_batch(parent_url=...)` are the brief's. The
mapping is the one function, `_job_from_row`. If you would rather the columns were renamed,
that is a one-line change in `JOB_TABLE_SQL` plus the mapping — but it would make the schema
diverge from §6, so I did not do it unilaterally.

**2. `deduplicated` holds the **holder's** id, not the dropped leaf's.** The dropped leaf has
no row, so its id would identify nothing; the UI needs an id it can link "already queued as #12"
to. When the collision is with a sibling leaf of the same batch, the holder is the row the
same call created milliseconds earlier.

**3. `create_batch` returns only `created` and `deduplicated`; `skipped` is always empty.**
As you directed: a track already on disk is found at execution time by Task 9's dedup check,
because a queued job can sit long enough for the file to be deleted underneath it, and a
filesystem check here would be a second duplicate check with different timing for the two to
disagree about. `skipped` is kept because §9's response shape is
`{created[], skipped[], deduplicated[]}` and Task 9 fills it from the execution-time result.
There is **no filesystem access in this module at all.**

**4. A leaf with an empty or `None` `adam_id`/`codec` is refused with `ValueError`.** Two
failure directions, and the second is the dangerous one. `""` would collide with every other
`""` across albums, codecs and URLs. `None` is quieter: **SQLite treats every NULL as distinct
in a unique index**, so a NULL `adam_id` neither deduplicates against anything nor prevents a
second NULL row — the index would look present and silently not apply. §6's column is nullable,
so this refusal is the only place that hole is closed. Same reasoning, same direction as
`dedup.find_duplicate`'s empty-key refusal: a re-download is recoverable, a false skip is not.

**5. A refused leaf stops the batch; the leaves before it stay.** There is nowhere in a
`BatchResult` to report a leaf that was neither created nor deduplicated, and inventing a
fourth list for a case that should not occur is worse than a loud failure — so it raises. It
does **not** roll back (autocommit; the earlier leaves are already rows) and the leaves *after*
the bad one are not attempted. **This is the part Task 9 must know:** it is the only caller, so
it catches the `ValueError`, reads `list()`, and shows what is queued. A retry is safe, because
the index returns the already-enqueued leaves as `deduplicated`. The alternative — collect and
continue, then raise with a partial result — needs a new exception type carrying a payload,
which the brief's interface list has no room for. If Task 9 would rather have that, it is a
small change and I would rather it were asked for than invented here.

**6. `finished_at` is non-NULL exactly when the status is terminal; `started_at` belongs to
`claim_next` alone.** `finished_at` is derivable from the status, so deriving it is what makes
`POST /api/jobs/{id}/retry` (§9) land in a consistent row: re-queuing a failed job clears the
completion time without the caller remembering to. `started_at` and `finished_at` are
*rejected* as `mark` fields, so no caller can break either invariant. The four payload columns
are written only when passed, because only the caller knows whether it is pausing or
restarting a transfer — the store does not guess.

**7. `claim_next` clears `error`; `resume_waiting` does not.** Deliberately different, and the
asymmetry is the point. A job that has just started has no error yet, and leaving the previous
run's beside a `started_at` that is not that run's would put two runs in one row. But "the
Apple token expired" is the only record anywhere that the queue stalled for that reason, so a
resume keeps it; the next claim clears it. Pinned from both sides by
`test_resume_waiting_keeps_the_reason_the_queue_stopped`.

**8. `list(parent_id=None)` means "no filter", not "top level only".** §9's `?parent=` is
absent-means-no-filter like every other query parameter. Nothing distinguishes the two cases
today, because `create_batch`'s brief-pinned signature has no `parent_id` parameter and writes
`None` to every row; a sentinel would be a second spelling of "none" for a case that cannot
arise yet. The filter is still there and still tested, because the column is in §6 and
`Job.parent_id` is in the brief's dataclass.

**9. `parent_type` is a closed set of §6's five** (`song`, `album`, `artist`, `playlist`,
`music-video`), refused otherwise. Task 9 switches on it to decide between resolving a track and
resolving an album, so a typo would store cleanly and render as an unrecognised kind forever.
This is the same refusal discipline as `dedup.ARTIST_SCOPES` and `config._scope`. **Task 8
must not invent a sixth type** without a spec change.

**10. `mark` raises `JobNotFound` for an id that is not there**, rather than no-op'ing: a
silent no-op leaves a job the UI is still showing as running with nothing able to update it.
`get` returns `None` instead, because `GET /api/jobs/{id}` for an id the caller made up is a
404, not a bug in the caller.

**11. `close()` was added to `JobStore`.** Not in the brief's list, and needed both for
hygiene and for the test that proves the state is in the file rather than in the process. No
connection or cursor handle is exposed — see §4.

**12. `force` is required keyword-only**, as the brief pins it. A caller that forgets it
should not silently get `False`, since `force` changes what happens at execution time.

**13. `Leaf` is not `slots=True`.** The brief's own
`test_queue_dedup_ignores_language_and_force` copies a leaf with `vars()`, and `slots` removes
`__dict__`, making that a `TypeError` in a test that reads like a typo several files from the
cause. Frozen (a leaf's `adam_id`/`codec` are half the key, and must not change after it is
queued) but plain. Pinned by `test_the_leaf_is_not_slotted_so_vars_still_works`.

**14. Timestamps are UTC, offset-bearing, millisecond precision.** Milliseconds because that
is exactly what the ECMAScript *Date Time String Format* specifies, so `new Date(created_at)`
parses in the browser rather than relying on V8's leniency about 6-digit fractions.

**15. `JobStoreError` wraps a failure to open the database** and names the path. "unable to
open database file" on its own does not say which of a container's mounts is missing, and §3
puts this on a volume.

**16. `DEDUPE_INDEX_SQL` duplicates three status literals** rather than interpolating
`ACTIVE_STATUSES` — see §1; the duplication is pinned by the harness.

**17. The broker invents no snapshot.** A subscriber to a channel nothing has been published
on waits, rather than being handed `{"kind": "snapshot", "jobs": []}`. A fabricated empty
snapshot is indistinguishable from a real one and reads as good news. The snapshot is Task 9's
to publish, from a real `list()`. That is what the brief's own test asserts.

---

## 3. Test command and verbatim output

```
$ cd hub && uv run pytest tests/test_jobs.py -v
```

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 70 items

tests/test_jobs.py::test_create_batch_deduplicates_identical_leaves PASSED [  1%]
tests/test_jobs.py::test_queue_dedup_ignores_language_and_force PASSED   [  2%]
tests/test_jobs.py::test_queue_dedup_key_includes_codec PASSED           [  4%]
tests/test_jobs.py::test_a_finished_job_frees_the_dedupe_slot PASSED     [  5%]
tests/test_jobs.py::test_waiting_jobs_hold_the_dedupe_slot PASSED        [  7%]
tests/test_jobs.py::test_claim_next_is_exclusive PASSED                  [  8%]
tests/test_jobs.py::test_broker_delivers_to_a_late_subscriber_the_current_snapshot PASSED [ 10%]
tests/test_jobs.py::test_the_leaf_fields_are_the_ones_the_seam_and_the_resolver_import PASSED [ 11%]
tests/test_jobs.py::test_a_leaf_is_a_music_video_only_when_something_says_so PASSED [ 12%]
tests/test_jobs.py::test_a_leaf_cannot_be_edited_after_it_has_been_deduplicated PASSED [ 14%]
tests/test_jobs.py::test_the_leaf_is_not_slotted_so_vars_still_works PASSED [ 15%]
tests/test_jobs.py::test_deduplicated_names_the_job_that_holds_the_slot PASSED [ 17%]
tests/test_jobs.py::test_a_leaf_repeated_within_one_batch_is_deduplicated_against_its_sibling PASSED [ 18%]
tests/test_jobs.py::test_the_index_is_partial_so_two_finished_jobs_may_share_a_key PASSED [ 20%]
tests/test_jobs.py::test_every_terminal_status_frees_the_dedupe_slot[cancelled] PASSED [ 21%]
tests/test_jobs.py::test_every_terminal_status_frees_the_dedupe_slot[done] PASSED [ 22%]
tests/test_jobs.py::test_every_terminal_status_frees_the_dedupe_slot[failed] PASSED [ 24%]
tests/test_jobs.py::test_every_terminal_status_frees_the_dedupe_slot[skipped] PASSED [ 25%]
tests/test_jobs.py::test_every_active_status_holds_the_dedupe_slot[queued] PASSED [ 27%]
tests/test_jobs.py::test_every_active_status_holds_the_dedupe_slot[running] PASSED [ 28%]
tests/test_jobs.py::test_every_active_status_holds_the_dedupe_slot[waiting] PASSED [ 30%]
tests/test_jobs.py::test_the_holder_keeps_its_own_language_and_url PASSED [ 31%]
tests/test_jobs.py::test_force_is_stored_and_does_not_buy_a_second_slot PASSED [ 32%]
tests/test_jobs.py::test_is_music_video_is_not_part_of_the_key PASSED    [ 34%]
tests/test_jobs.py::test_a_batch_of_no_leaves_does_nothing PASSED        [ 35%]
tests/test_jobs.py::test_the_parent_url_and_type_are_stored_on_every_job PASSED [ 37%]
tests/test_jobs.py::test_a_key_with_no_identity_in_it_is_refused[-adam_id] PASSED [ 38%]
tests/test_jobs.py::test_a_key_with_no_identity_in_it_is_refused[-codec] PASSED [ 40%]
tests/test_jobs.py::test_a_key_with_no_identity_in_it_is_refused[None-adam_id] PASSED [ 41%]
tests/test_jobs.py::test_a_key_with_no_identity_in_it_is_refused[None-codec] PASSED [ 42%]
tests/test_jobs.py::test_a_refused_key_stops_the_batch_but_keeps_what_had_already_landed PASSED [ 44%]
tests/test_jobs.py::test_an_unknown_parent_type_is_refused PASSED        [ 45%]
tests/test_jobs.py::test_claim_next_on_an_empty_queue_is_none PASSED     [ 47%]
tests/test_jobs.py::test_claim_next_is_fifo PASSED                       [ 48%]
tests/test_jobs.py::test_claim_next_starts_exactly_one_job PASSED        [ 50%]
tests/test_jobs.py::test_claim_next_stamps_started_at_and_leaves_finished_at_alone PASSED [ 51%]
tests/test_jobs.py::test_claim_next_is_exclusive_under_real_concurrency PASSED [ 53%]
tests/test_jobs.py::test_claim_next_reports_an_empty_queue_instead_of_blocking PASSED [ 55%]
tests/test_jobs.py::test_two_stores_on_one_file_see_each_others_rows PASSED [ 56%]
tests/test_jobs.py::test_get_returns_none_for_an_unknown_id PASSED      [ 58%]
tests/test_jobs.py::test_mark_records_progress_and_leaves_the_fields_it_was_not_given PASSED [ 59%]
tests/test_jobs.py::test_mark_records_a_skip_reason_and_an_error PASSED [ 60%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[cancelled] PASSED [ 61%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[done] PASSED [ 62%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[failed] PASSED [ 64%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[queued] PASSED [ 65%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[running] PASSED [ 67%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[skipped] PASSED [ 68%]
tests/test_jobs.py::test_finished_at_is_set_exactly_for_the_terminal_statuses[waiting] PASSED [ 70%]
tests/test_jobs.py::test_re_queuing_a_finished_job_clears_its_finished_at PASSED [ 71%]
tests/test_jobs.py::test_mark_rejects_an_unknown_status PASSED           [ 72%]
tests/test_jobs.py::test_mark_rejects_an_unknown_field_and_changes_nothing PASSED [ 74%]
tests/test_jobs.py::test_marking_a_job_that_is_not_there_raises PASSED   [ 75%]
tests/test_jobs.py::test_list_orders_by_id_and_filters_by_status PASSED  [ 77%]
tests/test_jobs.py::test_list_rejects_an_unknown_status PASSED           [ 78%]
tests/test_jobs.py::test_list_has_no_top_level_filter_because_nothing_writes_a_parent_id PASSED [ 80%]
tests/test_jobs.py::test_resume_waiting_on_a_queue_with_nothing_waiting_is_zero PASSED [ 81%]
tests/test_jobs.py::test_a_resumed_job_is_claimed_again_rather_than_queued_behind_the_rest PASSED [ 82%]
tests/test_jobs.py::test_resume_waiting_keeps_the_reason_the_queue_stopped PASSED [ 84%]
tests/test_jobs.py::test_a_live_message_reaches_a_subscriber_that_is_already_waiting PASSED [ 85%]
tests/test_jobs.py::test_two_subscribers_both_receive_what_is_published_to_their_channel PASSED [ 87%]
tests/test_jobs.py::test_a_subscriber_finds_no_snapshot_on_a_channel_nobody_published_on PASSED [ 88%]
tests/test_jobs.py::test_a_message_published_after_the_backlog_and_before_the_live_wait_is_not_lost PASSED [ 90%]
tests/test_jobs.py::test_the_backlog_is_the_last_fifty_messages_and_no_more PASSED [ 91%]
tests/test_jobs.py::test_a_late_subscriber_is_not_pitched_another_channel_s_backlog PASSED [ 92%]
tests/test_jobs.py::test_a_publish_after_a_subscriber_was_cancelled_still_reaches_the_next_one PASSED [ 94%]
tests/test_jobs.py::test_a_subscriber_that_is_never_started_receives_nothing PASSED [ 95%]
tests/test_jobs.py::test_a_frame_is_one_sse_data_field_whatever_the_payload_contains PASSED [ 97%]
tests/test_jobs.py::test_a_payload_that_is_not_json_fails_at_publish_time PASSED [ 98%]
tests/test_jobs.py::test_every_public_object_survives_a_json_round_trip PASSED [100%]

============================== 70 passed in 0.30s ==============================
```

The brief's Step 2 red state, before either module existed:

```
tests/test_jobs.py:38: in <module>
    from hub.events import HISTORY, EventBroker
E   ModuleNotFoundError: No module named 'hub.events'
```

(`hub.events` rather than `hub.jobs` only because the import lines are alphabetical; it is the
same failure the brief predicted.)

Whole suite, and lint:

```
$ cd hub && uv run pytest -q
203 passed in 27.23s

$ uv run ruff check hub tests spike
Found 1 error.        # EXE001 in spike/child_process_probe.py, pre-existing, not this task
```

---

## 4. Why the schema is verified by a harness and not by the suite

`tests/` in this repo does not reach into an object's internals — I checked: no existing test
touches a private. The schema, however, is only observable through a raw connection
(`PRAGMA table_info`, `sqlite_master`, the pragmas), and you asked for `sqlite_master`
specifically. So the split is:

- **the suite** pins what the schema *does*, through the public API only — dedup semantics,
  partiality, the key, the closed sets, the timestamp invariant, exclusivity, persistence;
- **`spike/task7_schema_check.py`** does the raw-SQL half, prints it, and exits non-zero on any
  failed check so it can be a gate. It is the same arrangement tasks 4 and 5 used, and it
  means the store exposes no connection handle purely for a test's benefit.

The one thing I could not pin from either place: whether an unclosed subscriber is still being
written to. It is unobservable from outside the broker, so I did not write a test that asserts
it by reaching in — I pinned the part that *is* observable (a cancelled subscriber's
`CancelledError` does not escape, and the broker keeps working) and left the `finally` to
inspection. The `finally` in `subscribe` runs on `aclose()`, on `break`, and on cancellation.

---

## 5. The three verifications you asked for

```
$ cd hub && uv run python spike/task7_schema_check.py
```

### A. The schema, the index, and the pragmas, read back off the file

```
A. sqlite_master, as written to disk
------------------------------------

-- table job
CREATE TABLE job (
  id           INTEGER PRIMARY KEY,
  url          TEXT    NOT NULL,
  url_type     TEXT    NOT NULL,   -- song|album|artist|playlist|music-video
  adam_id      TEXT,
  title        TEXT,               -- log display only; never compared (spec 7.3)
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

-- index job_active_dedupe
CREATE UNIQUE INDEX job_active_dedupe
  ON job(adam_id, codec)
  WHERE status IN ('queued', 'waiting', 'running')

A.2 the index, as SQLite parsed it
----------------------------------
  [ok] the index exists
  [ok] job_active_dedupe is UNIQUE
  [ok] job_active_dedupe is PARTIAL -- partial=1
  [ok] its key is (adam_id, codec) -- ['adam_id', 'codec']

A.3 the pragmas, on a connection that ran none of our code
----------------------------------------------------------
  journal_mode = 'wal'
  [ok] journal_mode is wal -- got 'wal'
  foreign_keys = 0
  busy_timeout = 5000
```

Three things worth reading off that:

- The DDL is §6's, character for character, and the index's predicate is the spec's
  `status IN ('queued', 'waiting', 'running')` — `waiting` included, so a job parked on an
  expired Apple token is not re-run alongside a new one.
- `PRAGMA index_list` is the only pragma that reports `partial`; it reads `1`. `index_info`
  gives the key columns in `seqno` order. (`index_xinfo` has no `partial` column — I checked,
  and my first version of the harness asserted on one.)
- `journal_mode` is read back from a connection that never ran our code, which is the only way
  to show WAL is on the *file* rather than on the connection that set it. `foreign_keys` reads
  0 there because it is per connection — which is why `__init__` sets it on every store.
  `busy_timeout` reads 5000 because Python's own `sqlite3.connect(timeout=5.0)` installs that;
  it agrees with §6 and the store sets it explicitly anyway, so the value is a decision rather
  than a library default a future Python could change.

**`foreign_keys=ON` is currently inert**, and I want that on the record: nothing ever writes a
non-NULL `parent_id` (§2 decision 8), so the pragma has no constraint to enforce today. It is
set because §6 asks for it and the column exists, and it starts mattering the moment anything
writes a parent.

### B. `claim_next` exclusivity under real concurrency

16 threads, 16 jobs, **16 separate `JobStore` connections on one file**, all released from a
`threading.Barrier` at the same instant, each looping until `claim_next()` returns `None`:

```
B. claim_next exclusivity, 16 threads over 16 jobs
--------------------------------------------------
  claims per thread: [[], [], [], [], [], [], [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17], [], [], [], [], [], [], [], [2], []]
  total claims: 16   distinct ids: 16
  [ok] no thread raised
  [ok] exactly 16 claims in total -- got 16
  [ok] no id was claimed twice
  [ok] exactly 16 jobs are running -- got 16
  [ok] claim_next is exhausted
```

No lock is shared between the threads and the code under test, so the only thing that can stop
two threads claiming one job is the single-statement `UPDATE`. The same check runs in the suite
at 12 threads (`test_claim_next_is_exclusive_under_real_concurrency`), and I ran it three times
back to back to check it is not timing-dependent.

**I checked that the test has power, by breaking the implementation.** I replaced
`claim_next` with the read-then-write version everyone writes first (`SELECT ... LIMIT 1`, then
`UPDATE ... WHERE id = that`) and re-ran:

```
    --- the suite on the mutant ---
    E       AssertionError: expected 12 claims in total, got 24
    E         +  where 24 = len([1, 1, 1, 1, 1, 2, ...])
    FAILED tests/test_jobs.py::test_claim_next_is_exclusive_under_real_concurrency
    FAILED tests/test_jobs.py::test_claim_next_reports_an_empty_queue_instead_of_blocking
    2 failed, 68 passed

    --- the harness on the mutant ---
    total claims: 38   distinct ids: 16
    [FAIL] exactly 16 claims in total -- got 38
    [FAIL] no id was claimed twice

    --- the brief's own test on the mutant ---
    tests/test_jobs.py::test_claim_next_is_exclusive .                        [100%]
    1 passed
```

Job 1 was handed out **five times** by the mutant. And the brief's own
`test_claim_next_is_exclusive` **passes on the broken implementation** — one connection, one
thread, and any two consecutive claims differ. Your instinct was right and the numbers show
it. The implementation was then restored and re-verified green.

### C. A duplicate `(adam_id, codec)` while another job is active

Hand-written `INSERT` on a raw connection, with job 1 already queued for `(adam_id=1,
codec=alac)`:

```
C. a hand-written duplicate (adam_id, codec) while another job is active
------------------------------------------------------------------------
  store created job 1 for (adam_id=1, codec=alac)

  sqlite3.IntegrityError: UNIQUE constraint failed: job.adam_id, job.codec
  [ok] a duplicate active key is refused
  [ok] the error names exactly the two key columns -- UNIQUE constraint failed: job.adam_id, job.codec
  [ok] and names neither language nor force, which are not in the key

  create_batch again -> created=[] deduplicated=[1]
  [ok] create_batch turned it into a deduplicated entry
  [ok] and it names the job that holds the slot
  [ok] no second row was written
  after mark(done), create_batch again -> created=[2] deduplicated=[]
  [ok] a finished job frees the slot
```

So: an `IntegrityError`, not an unhandled crash, and `create_batch` converts it into a
`deduplicated` entry naming job 1. No second row is written, and once job 1 is `done` the same
`create_batch` creates job 2 — the slot is released, not leaked.

One correction to a claim I made while writing this: SQLite names the violated **columns**, not
the index (`UNIQUE constraint failed: job.adam_id, job.codec`), so an error that does not
mention `job_active_dedupe` is expected rather than a gap. The check turned out to be better
this way — `language` and `force` being *absent* from the error is §6's "キー = (adam_id,
codec)。language と force はキーに含めない" appearing in a runtime error rather than only in a
comment.

### D. The broker, for completeness

```
D. the broker's backlog, as a browser connecting mid-download would see it
--------------------------------------------------------------------------
  'data: {"kind":"progress","id":0}\n\n'
  'data: {"kind":"progress","id":1}\n\n'
  'data: {"kind":"progress","id":2}\n\n'
  'data: {"kind":"progress","id":3}\n\n'
  'data: {"kind":"progress","id":4}\n\n'
  [ok] a late subscriber is replayed the backlog
  [ok] then goes live
  [ok] every frame is one data: line

result
------
  every check passed
```

---

## 6. Concerns for Task 9

1. **A refused leaf stops the batch** (§2 decision 5). `POST /api/jobs` must catch
   `ValueError` from `create_batch`, call `list()` to see what landed, and tell the user. An
   uncaught `ValueError` there is a 500 with 19 tracks silently queued behind it.

2. **The broker is not the snapshot.** Nothing publishes an initial queue; `subscribe` replays
   whatever was published. `GET /api/jobs/stream` must publish a real
   `{"kind": "snapshot", "jobs": [...]}` from `list()` immediately after subscribing, or a tab
   opened after the last event renders an empty queue.

3. **A connected-but-silent subscriber grows without bound.** `HISTORY = 50` bounds what a
   late subscriber is *replayed*, not what a live one accumulates. A background tab that stops
   reading grows its queue for the life of the process. I did not add a drop policy because it
   is per-channel and depends on what is being published (a snapshot stream can recover from a
   drop, a log stream cannot) — that decision is Task 9's, with the full picture.

4. **`publish` must be called from the event loop**, not from a worker thread. The one
   cross-thread candidate is `WrapperSupervisor.log_sink`, and Task 5's pump already runs on
   the loop, so this is fine as written; if a real thread appears, it needs
   `loop.call_soon_threadsafe`.

5. **`parent_id` is write-only dead weight for now.** The column, the dataclass field and the
   `list()` filter all exist and the filter is tested, but nothing sets a non-NULL value. §9's
   `GET /api/jobs?parent=` will always return everything until something populates it.

6. **There is no migration path yet, by design** (§2, and the module docstring). One table,
   created if absent. The first column added after this point needs a `PRAGMA user_version`
   gate, because `CREATE TABLE IF NOT EXISTS` will silently keep an old database's shape.

7. **The stale-`.pyc` trap in §0.** Clear `__pycache__` before believing any failure in this
   workspace; it made a correct test fail and `inspect.getsource` actively misleading.

8. **`test_a_key_with_no_identity_in_it_is_refused` is a resolver contract.** If Task 8 can
   produce a `Leaf` with an empty `adam_id` — from an album track the API did not identify —
   it will now raise rather than queue. The resolver should fall back to the parent URL's
   identifier, or Task 9 has to handle the `ValueError` from §6.1.

---

# Fix round 1

**Status:** DONE_WITH_CONCERNS · **Date:** 2026-09-27 · 5 Important + 9 minor, all addressed.
`hub/hub/jobs.py` 557→625, `hub/hub/events.py` 152→261, `tests/test_jobs.py` 873→1273,
`spike/task7_schema_check.py` 282→308. **87 tests in the file, 220 in the suite** (was 70/203).

The schema, the atomic `claim_next`, the partial index and its predicate, the three-outcome
design, the `ValueError`-after-partial-apply contract, the column mapping and the `Leaf` field
names are **untouched** — verified by `git diff` below.

One thing below is not a killed mutant but a *disproved* one (M3a), and it is the finding I
would most want a second opinion on.

---

## 1. I1 — the `IntegrityError` path, and a real defect it uncovered

**The implementation was wrong, not just untested.** The handler inferred "this was a
duplicate" from "a holder exists", and that inference is unsound. Reached with a trigger
standing in for a `NOT NULL` violation on a leaf whose key is already held, the pre-fix code
returned this:

```
BatchResult(created=[], skipped=[], deduplicated=[1])
```

A `deduplicated` entry naming job 1, with **no row written** — precisely the "user is told
their track was queued and nothing is running" outcome the comment claimed to prevent. The
fix is a **positive identification** rather than an inference:

```python
def _is_dedupe_violation(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc)
    return "UNIQUE constraint failed" in message and all(
        f"job.{column}" in message for column in ("adam_id", "codec")
    )
```

SQLite names the violated constraint's **columns**, so this asks the database which rule fired
instead of deducing it. The cost is stated in the docstring: if SQLite's wording ever changes,
this returns `False` for a real duplicate and the caller sees a raised `IntegrityError` — a
loud failure, not a silently dropped download.

**Three new tests**, all reaching the branch with a second `sqlite3.connect` on the same file
installing a trigger — a fault-injection device that needs no private access. Two details cost
a store's worth of care and are documented in the `poison_inserts` helper:

- The store is **reopened** after the trigger is created. SQLite hands a connection the
  statement it already compiled, and that compiled statement has no trigger in it — verified,
  including for a key that was never inserted before (the *statement* is cached, not the row).
- The occupying row is written **before** the trigger exists, because the interesting case is a
  failure on a leaf whose key is **already held**.

| test | forces | kills |
|---|---|---|
| `test_an_integrity_failure_that_is_not_the_dedup_index_is_raised` | `RAISE(ABORT, 'NOT NULL constraint failed: job.title')` | M1, M2, M3b, M4 |
| `test_a_sqlite_error_that_is_not_an_integrity_error_is_raised` | a call to a function that does not exist → `OperationalError` | M3b |
| `test_the_store_is_usable_after_a_rejected_insert` | drops the trigger, enqueues again | M3b, M4 |

The third is a bonus: it is the observable form of the autocommit invariant the whole dedup path
rests on — the second duplicate in a batch fails with "cannot start a transaction within a
transaction" if that ever regresses.

## 2. I2 — `parent_url` is validated, and the docstring is now true

`create_batch` refuses a `parent_url` that is not a non-empty string, with `ValueError`, before
the first leaf is touched. `JobStoreError` is gone from the docstring: it is raised only by
`__init__`, and it is now **stated** that it is not raised here, so no caller is told to catch
something unreachable. `create_batch`'s Raises paragraph now names all three real outcomes —
`ValueError` for the three argument kinds, and a propagating `sqlite3.Error` for a non-dedup
failure.

Three tests, including one that the refusal happens *before* any insert: otherwise the same
album would be half-enqueued and then fail, and the caller's retry would fold the half into
`deduplicated` for a request that never had a URL.

**I did not add `list(parent_url=...)`.** You corrected the plan, so Task 9 owns it, and
`test_list_has_no_top_level_filter_because_nothing_writes_a_parent_id` now says so explicitly
— that filtering a batch by `parent_id` is impossible today, which is *why* Task 9 needs the
parameter, and that adding one before the caller exists is how `parent_id` got into the
signature in the first place.

## 3. I3 — the live queues are bounded, and the overflow is a signal

`asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)` (1000, a keyword so Task 9 can size a log
channel differently), an explicit `if subscriber.queue.full()` branch in `publish` that counts
and **never raises**, and a `SubscriberOverrun` raised from inside the stream carrying
`channel`, `depth` and `limit`. The policy is deliberately not chosen here: the broker says how
far behind the reader is and stops, because it cannot know whether the stream is a queue state
(where a lost message costs nothing) or a log line (where it is the whole point).

One design detail worth your eye: the frame is taken **before** the overrun check.

```python
message = await subscriber.queue.get()
if subscriber.overrun:
    raise SubscriberOverrun(...)
yield message
```

A subscriber parked on `get()` is woken by the first message of a burst, so a check placed
first would hand over one stale frame before noticing. This order is what makes "the frames in
it are not delivered" true rather than nearly true — I found that by writing the test and
watching it fail.

Four tests: the overrun signal (depth 7 into a queue of 3), `publish` neither raising nor growing
(20 into a queue of 3, depth 17, history still whole), a healthy reader never told it overran,
and the default capacity taking a full `SUBSCRIBER_QUEUE_SIZE` burst silently.

## 4. I4 — the anti-leak `finally`, pinned on a deterministic path

I kept my round-0 reasoning that this is unobservable, and that is what forced a decision: the
only ways to see it are to read `_channels` or to give the broker a count. Given you asked for a
test, I added **`EventBroker.subscriber_count(channel) -> int`** — read-only, documented as
existing for exactly this reason, and the same trade `spike/task7_schema_check.py` is for the
schema. Three tests use it: unregistration on `aclose()` (with `StopAsyncIteration` proving the
generator is finished, not merely deregistered), on cancellation, and two subscribers counted
separately so closing one does not close the other. Removing the `finally` fails **5**.

## 5. I5 — `_holder`'s status filter, and the UI defect it prevents

`test_deduplicated_names_the_running_job_not_the_oldest_one`: job 1 `done`, job 2 `queued`, one
key. Without the filter, `fetchone()` returns **1** and the UI renders "already queued as #1"
for a job that will never run — the read-side twin of the write-side failure
`test_the_index_is_partial_…` already prevents. The `_holder` docstring now says why the filter
is *not* redundant with the index: the index constrains what may exist, not which of several
rows sharing a key over time a `fetchone()` should return.

## 6. Minors

- **1 — the stated evidence was wrong, and is now both corrected and made true.** The claim that
  the `ACTIVE_STATUSES`/`DEDUPE_INDEX_SQL` duplication is "pinned" named the wrong mechanism. The
  comment in `jobs.py` now names the two tests that actually pin it (each status on both sides
  of the boundary), and `spike/task7_schema_check.py` now **parses the predicate out of
  `sqlite_master`** and compares it as a set. All three of your counter-examples are killed:
  widen to `+'failed'` → killed, drop `'waiting'` → killed, narrow to `status='queued'` → killed
  (the last one crashed with an `IndexError` on the first attempt; the parse is now defensive and
  reports a clean failure).
- **2/3 — `foreign_keys` and `busy_timeout` are printed with what they actually are.** The
  harness now prints them as "per connection; this one is a SQLite default" rather than as
  evidence, and states the consequence plainly: `foreign_keys=ON` has **no end-to-end
  demonstration and cannot have one**, because nothing writes a non-NULL `parent_id`, so there
  is no way to ask the store to violate the reference. `busy_timeout = 5000` is Python's own
  `sqlite3.connect(timeout=5.0)` default, so the printed value is a regression guard, not proof
  of authorship. Both claims are now true rather than implied.
- **5 — `list()`'s ordering claim, in the case it argues for.** `create_batch` shares one
  `created_at` per batch, so the orderings agree by accident in every existing test. The new
  test moves two rows' timestamps apart with raw SQL, asserts `[1, 2, 3, 4]`, **and asserts the
  two orderings really differ** (`[3, 4, 1, 2]`) so the first assertion cannot be vacuous.
- **6 — `check_same_thread` pinned.** A foreign-thread call raises `sqlite3.ProgrammingError`,
  measured, with `thread.join(timeout=30)` and an assertion that the thread finished.
- **7 — the never-started generator is now held in a variable** and the count is asserted before
  *and* after the channel is used, so an eagerly-registering broker fails it for the right reason.
- **9 — a `running` row with a null `started_at` is measured, not assumed.** The harness counts
  them after its 16-thread claim run (`undated: []`), and `claim_next`'s comment records that it
  is the only writer of that column and that `mark` rejects it as a field, so Task 9 can render a
  start time with no null branch.
- **10 — no test can hang.** The two bare `__anext__` awaits now go through `settle()`;
  `parked()` wraps its single yield in `wait_for`; `PARK_TIMEOUT = 5.0`. Every read in the broker
  section goes through one of the three helpers.

---

## 7. Commands and verbatim output

### The suite

```
$ cd hub && uv run pytest tests/test_jobs.py -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 87 items
...
============================== 87 passed in 0.32s ==============================
```

```
$ cd hub && uv run pytest -q
220 passed in 27.29s

$ cd hub && uv run ruff check hub/jobs.py hub/events.py tests/test_jobs.py spike/task7_schema_check.py
All checks passed!
```

### The suite cannot hang — wall-clock timeout

```
$ time timeout 120 uv run pytest -q
....
220 passed in 27.34s
timeout 120 uv run pytest -q  2.54s user 0.86s system 12% cpu 27.541 total
```

27.5 s against a 120 s ceiling, and the broker section three times over:

```
$ for i in 1 2 3; do timeout 60 uv run pytest tests/test_jobs.py -q -k "broker or subscriber or backlog or frame or overrun or counted"; done
14 passed, 72 deselected in 0.18s
14 passed, 72 deselected in 0.18s
14 passed, 72 deselected in 0.18s
```

### The harness

```
$ uv run python spike/task7_schema_check.py
...
  [ok] its predicate is exactly the active statuses -- ['queued', 'running', 'waiting'] vs ['queued', 'running', 'waiting']

A.3 the pragmas
---------------
                  journal_mode = 'wal'   (read from a connection that ran no store code)
  [ok] journal_mode is wal -- got 'wal'
                  foreign_keys = 0   (per connection; this one is a SQLite default)
                  busy_timeout = 5000   (per connection; this one is a SQLite default)
...
  [ok] every running job has a started_at -- undated: []
...
result
------
  every check passed
EXIT=0
```

---

## 8. The six mutations

Applied with a script that copies the file, patches it, runs `pytest tests/test_jobs.py -q`,
and restores from the copy in a `finally` — so the tree is intact whatever happens.

| # | mutant | result |
|---|---|---|
| M1 | drop the holder check, append a fake id (`-1`) | **KILLED** — 5 failed, 82 passed |
| M2 | append to `created` instead of `deduplicated` | **KILLED** — 11 failed, 76 passed |
| M3b | broaden to `sqlite3.Error` **and** drop the positive identification | **KILLED** — 3 failed, 83 passed |
| M3a | broaden to `sqlite3.Error` **only** | **SURVIVED** — 87 passed. See below. |
| I3 | remove the `QueueFull` branch | **KILLED** — 2 failed, 85 passed (`asyncio.queues.QueueFull`) |
| I3b | make the queue unbounded | **KILLED** — 2 failed, 85 passed (`DID NOT RAISE SubscriberOverrun`) |
| I4 | remove the anti-leak `finally` | **KILLED** — 5 failed, 82 passed |
| I5 | remove `_holder`'s status filter | **KILLED** — 1 failed, 86 passed |
| M4 | drop only the positive identification (the pre-fix inference) | **KILLED** — 2 failed, 85 passed |
| minor 1a | widen the predicate with `'failed'` | **KILLED** (harness) |
| minor 1b | drop `'waiting'` from the predicate | **KILLED** (harness) |
| minor 1c | narrow the predicate to `status='queued'` | **KILLED** (harness) |

### M3a survived, and I think that is the right outcome — but it is your call

Broadening the catch **while keeping `_is_dedupe_violation`** is not a defect any more, and
cannot be: the message check is the gate, and a `database is locked` or a closed connection
does not match it, so the broadened handler re-raises. The harm you described is gone by
construction rather than by test. I could not write a test that distinguishes it, because there
is no observable difference — and I did not want to write one that asserts on a `except` clause's
width, which would be a test of the implementation's shape rather than of its behaviour.

The sequence that does kill it is M3b, and it is the pre-fix code plus your mutation: **3
failed**, including both new I1 tests. So the finding is closed in the sense that matters — the
exact defect you described (`database is locked` during enqueue reported as "already queued")
is now impossible, and the test that would have caught it exists and is green.

If you would rather the catch width itself be pinned, the only honest way I can see is to drop
`_is_dedupe_violation` and go back to inferring from the holder, which is what introduced the
false `deduplicated` in §1. I would not trade one for the other.

---

## 9. Concerns

1. **`_is_dedupe_violation` depends on SQLite's error wording.** Stated in the docstring with the
   failure mode (loud, not silent), but it is a real coupling to a library's message format. It is
   the price of identifying the rule positively instead of by inference.
2. **`subscriber_count` is new public surface**, added because I4 is otherwise untestable. I
   weighed reading `_channels` in a test instead and preferred a method that admits what it is
   for. It is read-only and not part of anything the brief pins.
3. **`SUBSCRIBER_QUEUE_SIZE = 1000` is a number I chose** with a stated rationale, not a
   measurement — I have not run a browser against this. Task 9 should revisit it once it knows
   the publish rate; the overrun is the signal that it was too small, and a healthy reader is the
   signal that it was too large.
4. **`parent_url` validation could be stricter than Task 8 expects** if the resolver ever passes a
   non-`str` (an `AnyUrl`, say). `str(AnyUrl)` is what §9's JSON will hold, so I would rather it
   be refused at the boundary than stored as a URL-shaped object.
5. **The trigger-based fault injection needs the store reopened**, which makes those three tests
   sensitive to statement caching. If a future SQLite changes when it re-reads the schema, they
   will fail loudly (the trigger simply will not fire) rather than pass vacuously — but they are
   the only tests that depend on a driver detail.
