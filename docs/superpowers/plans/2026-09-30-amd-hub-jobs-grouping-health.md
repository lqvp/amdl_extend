# amd-hub Jobs Grouping, Scheduler Extraction, Export & Health Banner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the scheduler from app.py unchanged, add batch (parent_url) job operations, CSV/JSON job export, and a conditional health banner to amd-hub.

**Architecture:** `hub/` is a single-process FastAPI app whose state (`HubState`, slots dataclass) is passed whole to components. C moves the scheduling concern into `hub/scheduler.py` (`Scheduler` class, `forward_progress` public). A adds store-level batch UPDATEs on `parent_url` following the existing `requeue`/`refused` contract. E adds one read-only export route. F is pure UI consuming the existing `/api/status` shape.

**Tech Stack:** Python 3.13, FastAPI, sqlite3 (stdlib, autocommit), Jinja2, pytest (655 tests, `cd hub && uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-09-30-amd-hub-jobs-grouping-health-design.md`

## Global Constraints

- Execution order is strictly C → A → E → F; each phase ends with `cd hub && uv run pytest` green before the next begins.
- Before every test run: `find hub -name __pycache__ -type d -exec rm -rf {} +` and use `uv run` from `hub/` (host python3 is 3.12 and hangs supervisor tests).
- `src.*` may be imported only by `ripper_host.py` and `vendor.py` (AST-enforced; new files are scanned automatically).
- `HubState` fields are frozen — do not add or remove slots fields (`tests/test_state.py:122`).
- Terminal→active transitions stay illegal; all status writes keep guards inside the UPDATE statement (no read-then-write TOCTOU).
- New routes must be session-authenticated and appear in `tests/test_api_jobs.py` route-table assertions; responses inherit `SECURITY_HEADERS` middleware automatically.
- Commits use Conventional Commits (English), one logical change per commit.

## Review Focus

- **`running` row concurrent with a batch cancel** — the cancel must report it in `refused` and never touch the row (a mid-rip kill corrupts upstream's partial file).
- **Non-ASCII album names in export** — CSV must open correctly in Excel; requires BOM + UTF-8; no mojibake in download.
- **Unknown / empty `parent_url` in group operations** — cancel/requeue/delete answer 200 with empty lists (nothing to do is not an error); a *missing or non-string* `parent_url` in a cancel body answers 400 (silently acting on all rows is the wrong fallback).
- **Empty-but-mounted library root (drive unplugged)** — the banner says the root is empty; a *healthy* state draws no banner element at all (an always-present panel that reads "OK" can be ignored — the spec bans it).
- **Export while jobs mutate** — export is a snapshot read; rows that change state after the snapshot are reported as of read time, and the endpoint must not hold the write path.

---

### Task 1: Scheduler extraction (C — behavior-preserving move)

**Files:**
- Create: `hub/hub/scheduler.py`
- Modify: `hub/hub/app.py` (delete moved code, wire `Scheduler`)
- Test: existing `hub/tests/test_api_jobs.py` (scheduler sections), `hub/tests/test_state.py` — no new tests

**Interfaces:**
- Consumes: `HubState` (unchanged fields), `JobStore`, `LeafRegistry`, `RipperHost`, `WrapperSupervisor`, `EventBroker`
- Produces: `Scheduler` class in `hub/hub/scheduler.py`:
  - `Scheduler(state: HubState)` — holds `state`
  - `await Scheduler.run() -> None` — the old `scheduler_loop` body
  - `Scheduler.forward_progress(progress: Progress) -> None` — the seam callback; resolves `job_id = state.current_job` internally (the seam contract is `(Progress) -> None`, so it cannot take job_id)
  - `await Scheduler.drain() -> None` — the old `_drain`
  - Module constants move: `IDLE_POLL_SECONDS`, `IDLE_READINESS_POLL_SECONDS`, `POST_JOB_POLL_SECONDS`, `DRAIN_TIMEOUT_SECONDS`, `WRAPPER_GUARD_POLL_SECONDS`
  - `app.py` keeps: `download_root_from_format`, `validate_download_root`, `vendor_config_path`, `create_app`, `_jobs_counts`, `_log`, `main`, lifespan.

- [ ] **Step 1: Baseline green.** `cd hub && uv run pytest -q`. Record the pass count. Also record `grep -c "^async def scheduler_loop" hub/hub/app.py` (must be 1).

- [ ] **Step 2: Move the code.** Copy the listed functions/constants (app.py lines per the spec table) into `hub/hub/scheduler.py` as `Scheduler` methods; rewrite `state.loop.call_soon_threadsafe(...)` progress dispatch into `Scheduler.forward_progress`. In `app.py`, delete the moved code, import `Scheduler`, and in lifespan construct `state.scheduler = Scheduler(state)`, pass `state.scheduler.forward_progress` where `_on_progress(state)` was injected into the ripper, and start `state.scheduler.run()` as the task `scheduler_loop(state)` used to be. `Scheduler.run()` is now already the `state.scheduler` attribute the seam tests reference — check `test_state.py:122` passes unchanged.

- [ ] **Step 3: Verify unchanged behavior.** `cd hub && uv run pytest -q` — expect the same pass count as Step 1, zero failures. `grep -c "def scheduler_loop\|def run_pool\|def _execute" hub/hub/app.py` is 0.

- [ ] **Step 4: Commit.**
```bash
git add hub/hub/scheduler.py hub/hub/app.py
git commit -m "refactor: extract Scheduler from app.py without behavior change"
```

---

### Task 2: Group operations in the job store (A — storage layer)

**Files:**
- Modify: `hub/hub/jobs.py` (new `cancel_pending`, `RequeueResult`-shaped `CancelResult`, `parent_url` params on `requeue`/`delete_finished`)
- Test: `hub/tests/test_jobs.py`

**Interfaces:**
- Consumes: existing `JobStore` (autocommit connection, `_is_dedupe_violation`, `TERMINAL_STATUSES`)
- Produces:
  - `CancelResult` frozen dataclass, fields `cancelled: list[int]`, `refused: list[int]` (mirror `RequeueResult`)
  - `JobStore.cancel_pending(parent_url: str) -> CancelResult` — UPDATE `queued`/`waiting` → `cancelled` for that `parent_url` in one statement; `running` rows of the same `parent_url` listed in `refused`, untouched
  - `JobStore.requeue(statuses, parent_url: str | None = None) -> RequeueResult` — filter added to both the UPDATE and the refused-list SELECT
  - `JobStore.delete_finished(parent_url: str | None = None) -> list[int]` — filter added to both the doomed-id SELECT and the DELETE

- [ ] **Step 1: Write the failing tests** in `hub/tests/test_jobs.py`:

```python
def test_cancel_pending_cancels_queued_and_waiting_and_refuses_running(store):
    parent = "https://music.apple.com/us/album/x/123"
    queued = store.create_batch(url=parent, leaves=[...])[0]   # follow test_jobs.py fixture style
    running = store.create_batch(url=parent, leaves=[...])[0]
    store.claim_next()  # make `running` actually running
    other = store.create_batch(url="https://music.apple.com/other", leaves=[...])[0]
    result = store.cancel_pending(parent)
    assert sorted(result.cancelled) == sorted(r.id for r in [queued, waiting_or_the_rest])
    assert running.id in result.refused
    assert store.get(other.id).status == "queued"  # other groups untouched
    assert store.get(running.id).status == "running"

def test_cancel_pending_unknown_parent_url_cancels_nothing(store):
    result = store.cancel_pending("https://music.apple.com/absent")
    assert result.cancelled == [] and result.refused == []
```

Follow the file's existing fixture helpers for row creation; assert exactly the observable contract, in the file's naming style.

- [ ] **Step 2: Run to verify failure.** `uv run pytest tests/test_jobs.py -k cancel_pending -v` → fails (no `cancel_pending`).

- [ ] **Step 3: Implement** the three store changes per the Interfaces block. `cancel_pending` selects the `running` ids for `refused` *after* the atomic UPDATE (report-only; it may not be the exact instant-of-cancel set — say so in a comment). Guard stays in the UPDATE's WHERE; add docstrings that name *why* `running` is refused (in-flight transfer, upstream owns its partial file) mirroring `delete_finished`'s reasoning.

- [ ] **Step 4: Run to verify pass.** `uv run pytest tests/test_jobs.py -v` (full file, not just -k) → all pass.

- [ ] **Step 5: Commit.**
```bash
git add hub/hub/jobs.py hub/tests/test_jobs.py
git commit -m "feat: cancel_pending and parent_url filters in the job store"
```

---

### Task 3: Group operations over HTTP (A — API layer)

**Files:**
- Modify: `hub/hub/api/jobs.py` (new `POST /api/jobs/cancel`; `parent_url` on `requeue` body and `finished` query)
- Modify: `hub/hub/jobs.py` / `hub/hub/api/jobs.py` module docstrings (the "parent_id is nothing writes" notes → state the group key is `parent_url`)
- Test: `hub/tests/test_api_jobs.py`

**Interfaces:**
- Consumes: Task 2's `JobStore.cancel_pending`, `requeue(statuses, parent_url)`, `delete_finished(parent_url)`; `JOBS_CHANNEL`, `_publish_job`, `queue-control`-style publish helpers
- Produces: `POST /api/jobs/cancel` — session-authenticated; body `{"parent_url": str}`; 200 `{"cancelled": [int], "refused": [int]}`; publishes a `{"kind": "batch", "updated": [...], "deleted": []}`-shaped WS frame per changed id via existing `_publish_job`, and `{"kind": "deleted", "ids": [...]}` after group `delete_finished`. 400 when `parent_url` is missing or empty.

- [ ] **Step 1: Update the route-table test first.** In `hub/tests/test_api_jobs.py` the route table is derived from `app.openapi()`; update the expected route count (32→33) and confirm the new route is *not* in `OPEN_WITHOUT_A_SESSION` — run `uv run pytest tests/test_api_jobs.py -k openapi` (or the route-table test name) and watch it fail listing `POST /api/jobs/cancel` as session-required.

- [ ] **Step 2: Write behavior tests** (in the file's existing fake-supervisor pattern):

```python
async def test_cancel_endpoint_reports_cancelled_and_refused(client):  # follow existing client fixtures
    ... enqueue 1 queued + 1 running under parent_url ...
    r = await client.post("/api/jobs/cancel", json={"parent_url": parent})
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] and body["refused"]
    assert all(jobs[jid].status == "cancelled" for jid in body["cancelled"])

async def test_cancel_endpoint_requires_parent_url(client):
    r = await client.post("/api/jobs/cancel", json={})
    assert r.status_code == 400
```
Plus: `requeue` with `parent_url` only touches that group; `DELETE /api/jobs/finished?parent_url=` deletes only that group and forgets those leaves; unauthenticated cancel is 401.

- [ ] **Step 3: Run to verify failure**, then **implement** `cancel_jobs` handler (Pydantic body model mirroring `_RequeueBody`; validation errors → existing `fail(400, ...)`), the `parent_url` pass-through in `requeue_jobs` and `delete_finished_jobs`, and the WS publishes.

- [ ] **Step 4: Run to verify pass.** `uv run pytest tests/test_api_jobs.py -v` → all pass.

- [ ] **Step 5: Update the "nothing writes parent_id" comments** in `jobs.py`/`api/jobs.py` to name `parent_url` as the group key.

- [ ] **Step 6: Commit.**
```bash
git add hub/hub/api/jobs.py hub/hub/jobs.py hub/tests/test_api_jobs.py
git commit -m "feat: parent_url group cancel/requeue/delete endpoints"
```

---

### Task 4: Group action buttons in the queue UI (A — UI)

**Files:**
- Modify: `hub/hub/web/static/app.js`, `hub/hub/web/templates/queue.html` (group header actions), `hub/hub/web/static/app.css`
- Test: `hub/tests/test_web_contract.py`

**Interfaces:**
- Consumes: `POST /api/jobs/cancel`, `POST /api/jobs/requeue` (Task 3)
- Produces: `data-action` values `cancel-group` and `requeue-failed-group` handled in `app.js`; markup contract recognized by `test_web_contract.py`.

- [ ] **Step 1: Add the affordance tests** in `test_web_contract.py` following its existing affordance-correspondence pattern: every `data-action` in templates has a handler in `app.js`, and vice versa.
- [ ] **Step 2: Run `uv run pytest tests/test_web_contract.py -v`** → fails on the new actions.
- [ ] **Step 3: Implement** the group action column (visible only on group headers with a parent URL) and the `app.js` handlers (POST with JSON body, then the WS `batch`/`job` frames update rows — no manual DOM bookkeeping).
- [ ] **Step 4: Verify pass**: `uv run pytest tests/test_web_contract.py tests/test_api_jobs.py -v`, then full `uv run pytest -q` green.
- [ ] **Step 5: Commit.**
```bash
git commit -am "feat: per-group cancel and requeue actions on the queue page"
```

---

### Task 5: Export endpoint (E — API)

**Files:**
- Modify: `hub/hub/api/jobs.py` (new `GET /api/jobs/export`)
- Test: `hub/tests/test_api_jobs.py`

**Interfaces:**
- Consumes: `JobStore.list()` (or direct SELECT), `ACTIVE_STATUSES`, `TERMINAL_STATUSES`
- Produces: `GET /api/jobs/export?kind=history|queue&format=csv|json` — session-authenticated; queue = active rows id ASC, history = terminal rows id DESC; csv = BOM+UTF-8, `Content-Disposition: attachment; filename="amd-hub-<kind>-<UTC-YYYYMMDDTHHMMSS>Z.<ext>"`; json = list of row objects (all schema columns verbatim, `skip_reason` included as-is); invalid kind/format → 400.

- [ ] **Step 1: Update route-table expectations** (33→34) and watch it fail.
- [ ] **Step 2: Write behavior tests:**

```python
async def test_export_queue_csv_starts_with_bom_and_header(client):
    r = await client.get("/api/jobs/export?kind=queue&format=csv")
    assert r.content.startswith(b"\xef\xbb\xbf")
    assert "id,adam_id,codec" in r.text  # assert against the real header list

async def test_export_history_returns_terminal_rows_desc(client): ...
async def test_export_invalid_kind_is_400(client): ...
async def test_export_json_contains_skip_reason_resolved_paths(client): ...

async def test_export_leaves_the_queue_untouched(client):
    before = snapshot_of_jobs(client)          # ids, statuses, started_at
    await client.get("/api/jobs/export?kind=history&format=csv")
    await client.get("/api/jobs/export?kind=queue&format=json")
    assert snapshot_of_jobs(client) == before  # export is read-only
```

- [ ] **Step 3: Implement** the handler (single SELECT per call, no cache, no writes).
- [ ] **Step 4: `uv run pytest tests/test_api_jobs.py -v` green**, then full suite.
- [ ] **Step 5: Commit:** `git commit -am "feat: csv/json export of job history and queue"`

---

### Task 6: Export download links in the UI (E — UI)

**Files:**
- Modify: `hub/hub/web/templates/queue.html`, `hub/hub/web/static/app.css`
- Test: `hub/tests/test_web_contract.py`

**Interfaces:** Consumes: `GET /api/jobs/export` (Task 5). Produces: static `<a href="/api/jobs/export?...">` links (session cookie rides automatically; no JS).

- [ ] **Step 1: Add markup/contract test** in `test_web_contract.py` (link present, href exact).
- [ ] **Step 2: Verify failure**, **Step 3: implement** links (history CSV/JSON pair + queue download), **Step 4:** `uv run pytest tests/test_web_contract.py -v` green.
- [ ] **Step 5: Commit:** `git commit -am "feat: export download links on the queue page"`

---

### Task 7: Conditional health banner (F)

**Files:**
- Modify: `hub/hub/web/static/app.js`, `hub/hub/web/templates/base.html` (banner mount point), `hub/hub/web/static/app.css`
- Test: `hub/tests/test_web_contract.py`

**Interfaces:**
- Consumes: `GET /api/status` shape exactly: `wrapper.problem` (`None|"no-account"|"unavailable"|classify result`), `library.degraded_roots: list[str]`, `library.per_root: list[{...albums...}]`; existing WS `wrapper` frame.
- Produces: banner region rendered client-side from `/api/status`; `role="status"` normally, `role="alert"` for wrapper problems; no element in the DOM when healthy.

- [ ] **Step 1: Add contract tests** in `test_web_contract.py`: banner logic maps `wrapper.problem == "no-account"` → login-prompt text; `degraded_roots`/0-album `per_root` → root names; healthy status produces no banner node; `role` attributes as specified. (Follow the file's existing app.js source-inspection + template-rendering patterns.)
- [ ] **Step 2: Verify failure**, **Step 3: implement** (fetch on page load; refetch on WS reconnect and on `wrapper`-frame receipt — no polling, no cache), **Step 4:** `uv run pytest tests/test_web_contract.py -v` green, then full suite.
- [ ] **Step 5: Commit:** `git commit -am "feat: health banner for wrapper, degraded roots and empty mounts"`

---

### Task 8: Whole-branch validation

- [ ] **Step 1: Full suite on 3.13:** `find hub -name __pycache__ -type d -exec rm -rf {} + ; cd hub && uv run --locked pytest -q` → green (CI-equivalent command).
- [ ] **Step 2: Deployment invariants unchanged:** `cd hub && uv run pytest tests/test_deployment.py -v` green (route changes must not have touched deployment assertions).
- [ ] **Step 3: Mutation check** (proves the deployment assertions can still fail): `cd hub && uv run python hub/deploy/mutation_check.py` per its usage in the file docstring; expected: every mutation caught.
- [ ] **Step 4: Commit any leftovers; report** changes summary to the operator (banner behavior, new endpoints, no data migration needed).
