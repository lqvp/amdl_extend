"""Task 7's schema/claim/dedup verification, against a real database file.

The test suite asserts what the schema *does*, through the public API, because that is what
the rest of the hub depends on. Three things it cannot reach that way, and this harness
covers them with raw SQL:

- **the DDL itself** -- `sqlite_master` for the table and the partial index, and the three
  pragmas, so spec §6's text can be read back off the file rather than taken on trust;
- **what the index does to a statement the store did not write** -- a duplicate
  `(adam_id, codec)` inserted by hand, which is the `IntegrityError` `create_batch` catches
  and converts into a `deduplicated` entry;
- **`claim_next` under real concurrency** -- N threads, N connections, one file, released
  from a barrier at the same instant. A sequential loop cannot tell the single-statement
  `UPDATE` from a read-then-write; threads can, and the number it prints is the evidence.

    uv run python spike/task7_schema_check.py [db-path]

Exits non-zero if any check fails, so it can be run as a gate. The path defaults to a
temporary directory and is removed afterwards; pass a path to keep the database and open it
with `sqlite3`.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.events import EventBroker
from hub.jobs import ACTIVE_STATUSES, DEDUPE_INDEX_NAME, JobStore, Leaf

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


def leaf(adam_id: str, codec: str = "alac") -> Leaf:
    return Leaf(
        adam_id=adam_id,
        title=f"title {adam_id}",
        album_name="Album",
        artist_name="Artist",
        codec=codec,
        language="ja",
        url=f"https://music.apple.com/jp/song/x/{adam_id}",
        storefront="jp",
    )


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --- A: the schema on disk --------------------------------------------------


def report_schema(path: Path) -> None:
    section("A. sqlite_master, as written to disk")
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    try:
        for row in raw.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            " ORDER BY type DESC, name"
        ):
            print(f"\n-- {row['type']} {row['name']}")
            print(row["sql"])

        section("A.2 the index, as SQLite parsed it")
        (index,) = raw.execute(
            "SELECT * FROM sqlite_master WHERE name = ?", (DEDUPE_INDEX_NAME,)
        ).fetchall()
        check("the index exists", index is not None)
        listed = {
            row["name"]: row for row in raw.execute("PRAGMA index_list(job)").fetchall()
        }
        check("job_active_dedupe is UNIQUE", listed[DEDUPE_INDEX_NAME]["unique"] == 1)
        # `index_list` is the only pragma that reports `partial`. `index_info` gives the key
        # columns in `seqno` order; `index_xinfo` adds collation and sort direction but no
        # partiality, and appends a trailing rowid row.
        check(
            "job_active_dedupe is PARTIAL",
            listed[DEDUPE_INDEX_NAME]["partial"] == 1,
            f"partial={listed[DEDUPE_INDEX_NAME]['partial']}",
        )
        keyed = [
            row["name"]
            for row in sorted(
                raw.execute("PRAGMA index_info(job_active_dedupe)").fetchall(),
                key=lambda row: row["seqno"],
            )
        ]
        check("its key is (adam_id, codec)", keyed == ["adam_id", "codec"], str(keyed))

        # Round 1, Minor 1: the *predicate* used not to be checked here at all, so widening
        # it to include 'failed', dropping 'waiting', or narrowing it to status='queued' all
        # left this harness green. `DEDUPE_INDEX_SQL` writes its three statuses out rather
        # than interpolating ACTIVE_STATUSES, so nothing forced the two to agree. The
        # behaviour tests in tests/test_jobs.py do -- each status on both sides of the
        # boundary -- and this parses the predicate back out of the file as well, so a
        # hand-edited index is caught here too.
        predicate = index["sql"].partition("status IN (")
        statuses = set()
        if predicate[1]:
            statuses = {p.strip().strip("'") for p in predicate[2].split(")")[0].split(",")}
        check(
            "its predicate is exactly the active statuses",
            statuses == set(ACTIVE_STATUSES),
            f"{sorted(statuses)} vs {sorted(ACTIVE_STATUSES)}"
            if predicate[1]
            else "no `status IN (...)` predicate at all",
        )

        section("A.3 the pragmas")
        # journal_mode is a property of the FILE, so it is read back from a connection that
        # never ran our code -- which is the only way to show WAL is on the database and not
        # merely on the connection that set it.
        got = raw.execute("PRAGMA journal_mode").fetchone()[0]
        print(f"  {'':>16}journal_mode = {got!r}   (read from a connection that ran no store code)")
        check("journal_mode is wal", got == "wal", f"got {got!r}")

        # foreign_keys and busy_timeout are per connection, so this connection is not evidence
        # of anything the store did -- it reads the SQLite defaults. What *is* evidence is
        # that the value a fresh JobStore gets is the one spec 6 names, and for busy_timeout
        # that is indistinguishable from Python's own default, which is stated rather than
        # glossed: sqlite3.connect(timeout=5.0) installs 5000 itself. It is checked as a
        # regression guard, not as proof of authorship.
        for pragma, expected in (("foreign_keys", 1), ("busy_timeout", 5000)):
            got = raw.execute(f"PRAGMA {pragma}").fetchone()[0]
            print(f"  {'':>16}{pragma} = {got!r}   (per connection; this one is a SQLite default)")
        print(
            "\n  foreign_keys=ON has no end-to-end demonstration here, and cannot have one:"
            "\n  nothing writes a non-NULL parent_id, so there is no way to ask the store to"
            "\n  violate the reference. It is set because spec 6 requires it and the column"
            "\n  exists; the constraint starts mattering the moment a row carries a parent."
        )
    finally:
        raw.close()


# --- C: the IntegrityError, and what create_batch does with it --------------


def report_duplicate_insert(path: Path) -> None:
    section("C. a hand-written duplicate (adam_id, codec) while another job is active")
    store = JobStore(path)
    first = store.create_batch("u", "album", [leaf("1")], force=False)
    print(f"  store created job {first.created[0]} for (adam_id=1, codec=alac)")

    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        try:
            raw.execute(
                "INSERT INTO job (url, url_type, adam_id, title, codec, language, force,"
                " status, created_at)"
                " VALUES ('u', 'album', '1', 't', 'alac', 'ja', 0, 'queued', '2026-01-01')"
            )
            check("a duplicate active key is refused", False, "the INSERT was accepted")
        except sqlite3.IntegrityError as exc:
            print(f"\n  sqlite3.IntegrityError: {exc}")
            check("a duplicate active key is refused", True)
            # SQLite names the *columns* of a violated unique index, not the index itself,
            # so the message is the evidence for the key: it lists `adam_id` and `codec` and
            # nothing else. `language` and `force` being absent from it is spec section 6's
            # "key = (adam_id, codec); language and force are not in the key" showing up in
            # a runtime error rather than only in a comment.
            message = str(exc)
            check(
                "the error names exactly the two key columns",
                "job.adam_id, job.codec" in message,
                message,
            )
            check(
                "and names neither language nor force, which are not in the key",
                "language" not in message and "force" not in message,
            )
    finally:
        raw.close()

    again = store.create_batch("u", "album", [leaf("1")], force=False)
    print(f"\n  create_batch again -> created={again.created} deduplicated={again.deduplicated}")
    check("create_batch turned it into a deduplicated entry", again.created == [])
    check("and it names the job that holds the slot", again.deduplicated == first.created)
    check("no second row was written", len(store.list()) == 1)

    store.mark(store.claim_next().id, "done")
    after = store.create_batch("u", "album", [leaf("1")], force=False)
    print(
        f"  after mark(done), create_batch again -> created={after.created}"
        f" deduplicated={after.deduplicated}"
    )
    check("a finished job frees the slot", after.created != [])
    store.close()


# --- B: claim_next under real concurrency -----------------------------------


def report_concurrent_claim(path: Path, jobs: int = 16) -> None:
    section(f"B. claim_next exclusivity, {jobs} threads over {jobs} jobs")
    JobStore(path).create_batch(
        "u", "album", [leaf(str(i)) for i in range(jobs)], force=False
    )
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
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    flat = [job_id for got in claimed for job_id in got]
    print(f"  claims per thread: {claimed}")
    print(f"  total claims: {len(flat)}   distinct ids: {len(set(flat))}")
    check("no thread raised", not failures, repr(failures[0]) if failures else "")
    check(f"exactly {jobs} claims in total", len(flat) == jobs, f"got {len(flat)}")
    check("no id was claimed twice", len(flat) == len(set(flat)))
    store = JobStore(path)
    running = [job.id for job in store.list() if job.status == "running"]
    check(f"exactly {jobs} jobs are running", len(running) == jobs, f"got {len(running)}")
    check("claim_next is exhausted", store.claim_next() is None)
    # Minor 9: nothing but a claim can put a row into `running`, and a claim always stamps
    # started_at, so Task 9 can render a start time with no null branch. A `running` row with
    # a null one is storable by raw SQL, so this is measured rather than assumed.
    undated = [j.id for j in store.list() if j.status == "running" and not j.started_at]
    check("every running job has a started_at", not undated, f"undated: {undated}")
    store.close()


# --- the broker, for completeness ------------------------------------------


async def report_broker() -> None:
    section("D. the broker's backlog, as a browser connecting mid-download would see it")
    broker = EventBroker()
    for index in range(4):
        broker.publish("jobs", {"kind": "progress", "id": index})
    agen = broker.subscribe("jobs")
    frames = []
    try:
        for _ in range(4):
            frames.append(await agen.__anext__())
        broker.publish("jobs", {"kind": "progress", "id": 4})
        frames.append(await agen.__anext__())
    finally:
        await agen.aclose()
    for frame in frames:
        print(f"  {frame!r}")
    check("a late subscriber is replayed the backlog", len(frames) == 5)
    check("then goes live", '"id":4' in frames[-1])
    check("every frame is one data: line", all(f.startswith("data: ") for f in frames))


def main() -> int:
    keep = len(sys.argv) > 1
    root = Path(sys.argv[1]) if keep else Path(tempfile.mkdtemp(prefix="task7-"))
    path = root / "hub.db"
    print(f"database: {path}")
    try:
        # The schema is created by the constructor, so it has to exist before there is
        # anything to read back. The store is closed again immediately: everything below
        # then looks at the file through connections that ran none of this module's code.
        JobStore(path).close()
        report_schema(path)
        report_duplicate_insert(path)
        report_concurrent_claim(path)
        import asyncio

        asyncio.run(report_broker())
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    section("result")
    if FAILURES:
        print(f"  {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("  every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
