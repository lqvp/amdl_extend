"""Task 9 fix round 1: the mutation checks the review said would have passed.

Every item here is a **swap that the round-0 suite would not have caught**, written out as a
patch, applied, and reverted. If applying a patch leaves the suite green, the fix is not
actually pinned and the test is not doing its job.

Run:  cd hub && uv run python spike/task9_fix1_check.py
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
PKG = HUB / "hub"

#: sha256 of each file this script may patch, captured when it starts. See `_dirty_paths`.
BASELINE: dict[str, str] = {}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def reopen_openapi(text: str) -> str:
    """C1: put the schema route back."""
    return text.replace("        openapi_url=None,\n", "")


def unguarded_static(text: str) -> str:
    """C2: mount the plain `StaticFiles`, outside the guard as it was."""
    text = text.replace(
        "    app.mount(\n        \"/static\",\n        GuardedStatic(directory=str(STATIC_DIR)),\n"
        '        name="static",\n    )',
        '    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")',
    )
    return text


def peer_to_forwarded(text: str) -> str:
    """I2: key the rate limiter on a header the caller chooses."""
    return text.replace(
        "    client = request.client\n"
        "    return client.host if client is not None and client.host else \"unknown\"",
        "    forwarded = request.headers.get(\"x-forwarded-for\") or \"\"\n"
        "    return forwarded.split(\",\")[0].strip() or \"unknown\"",
    )


def drop_generation_check(text: str) -> str:
    """I1: verify the signature but not the generation."""
    return text.replace(
        "        stamped = payload.get(\"g\")\n"
        "        if isinstance(stamped, bool) or not isinstance(stamped, int):\n"
        "            return False\n"
        "        return stamped == int(generation)",
        "        return True",
    )


def cumulative_frames(text: str) -> str:
    """I6: put the running totals back into the per-URL frame."""
    return text.replace(
        "        _publish_batch(\n"
        "            state, url=cleaned, created=this_created, deduplicated=this_deduplicated\n"
        "        )",
        "        _publish_batch(\n"
        "            state, url=cleaned, created=created, deduplicated=deduplicated\n"
        "        )",
    )


def fabricate_is_music_video(text: str) -> str:
    """I7: put the invented `False` back."""
    return text.replace(
        '    """A `Job` as JSON: `asdict`, and nothing added.',
        '    """PLACEHOLDER',
    ).replace(
        "    return asdict(job)",
        '    data = asdict(job)\n    data["is_music_video"] = False\n    return data',
    ).replace('    """PLACEHOLDER', '    """A `Job` as JSON: `asdict`, and nothing added.')


def wrong_tls_side(text: str) -> str:
    """I4: read the *last* forwarded value, as the old docstring claimed."""
    return text.replace(
        '    return forwarded.split(",")[0].strip().lower() == "https"',
        '    return forwarded.split(",")[-1].strip().lower() == "https"',
    )


def restore_always_sweep(text: str) -> str:
    """M4: sweep the whole table on every attempt."""
    return text.replace(
        "        if len(self._attempts) <= _PRUNE_THRESHOLD:\n            return\n",
        "",
    )


def fail_instead_of_park(text: str) -> str:
    """C4: never park, which is what §10 forbids.

    Round 3 widened `_park_reason` to a `(reason, detail)` pair, so the mutation unpacks it to
    a constant `None` reason: every failure becomes `failed`, which is the round-0 behaviour
    and the one the round-1 review said had no producer.
    """
    return text.replace(
        "        reason, detail = (\n"
        "            await _park_reason(state) if isinstance(exc, RipperHostError) else (None, \"\")\n"
        "        )",
        '        reason, detail = None, ""',
    )


def always_park(text: str) -> str:
    """C4's wrong discriminator, in the direction that silently breaks the queue.

    A substring match on the error's prose is the mutation the review named, and it needs a
    value to match on, so the *observable* form of it is modelled instead: park everything.
    That is the same bug with one fewer step -- every `RipperHostError` becomes `waiting`, and
    because `waiting` is not terminal, a job parked for a reason that never resolves is retried
    for ever and is never once visible as a failure. `test_a_genuine_download_failure_is_not_
    parked` is what catches it, and its error message deliberately mentions an account so a
    prose match would take it too.
    """
    return text.replace(
        "    if not state.supervisor.running:\n        return \"unavailable\", \"\"\n"
        "    try:\n"
        "        regions = (await state.supervisor.status()).get(\"regions\") or []\n"
        "    except Exception as exc:  # noqa: BLE001 - unknown is not ready, and the safe direction\n"
        "        return \"unreachable\", f\"{type(exc).__name__}: {exc}\"\n"
        "    return (None if regions else \"no-account\"), \"\"",
        '    return "unavailable", ""',
    )


def drop_progress_publish(text: str) -> str:
    """The assigned fix: write the progress but never publish it."""
    return text.replace(
        "    _publish_job(state, job_id)\n",
        "",
    )


def no_progress_mark(text: str) -> str:
    """The assigned fix: publish, but never write the row."""
    return text.replace(
        "        state.jobs.mark(\n"
        "            job_id,\n"
        '            "running",\n'
        "            progress=progress.fraction,\n"
        "            bytes_done=progress.bytes_done,\n"
        "            bytes_total=progress.bytes_total,\n"
        "        )\n",
        "        return\n",
    )


def trust_a_task_that_says_nothing(text: str) -> str:
    """The defect `spike/task9_contract_check.py` found: upstream reports failure by returning.

    `rip_song` catches everything, marks its `Task` FAILED and returns, so a caller that reads
    the return value as success marks every failed download `done`. The check is the fix; this
    mutation removes it, and the suite must notice.
    """
    return text.replace(
        "        if task.status in (_status_value(\"DONE\"), _status_value(\"ALREADY_EXIST\")):\n"
        "            return\n",
        "        return\n",
    )


def sink_reports_the_previous_task(text: str) -> str:
    """The sink keeps a stale task, so a retried download reads the last attempt's outcome."""
    return text.replace(
        '        """The task this rip left behind, or `None`. Removes it either way."""\n'
        "        return self._tasks.pop(adam_id, None)",
        '        """The task this rip left behind, or `None`."""\n'
        "        return self._tasks.get(adam_id)",
    )


def idle_poll_cached(text: str) -> str:
    """I5's other half: cache the readiness answer, which is §10's actual bug.

    A slow poll is only defensible because the *action* is guarded by a fresh check. Removing
    the check is the mutation the review's "a cache is only defensible if..." points at.
    """
    return text.replace(
        "        # Something is queued, so readiness is about to be acted on and is probed now. A\n"
        "        # cached answer here would be the §10 bug: a token that expired an hour ago would\n"
        "        # still read as ready.\n"
        "        problem = await _wrapper_problem(state)",
        "        problem = state.cached_problem\n"
        "        if problem is None:\n"
        "            problem = await _wrapper_problem(state)\n"
        "            state.cached_problem = problem",
    )


def slow_idle_poll(text: str) -> str:
    """I5: back to one HTTP probe per 0.5 s, unconditionally."""
    return text.replace(
        "        if not _has_actionable(state):\n"
        "            await _sleep_or_stop(state, IDLE_POLL_SECONDS)\n"
        "            continue\n",
        "",
    )


# --------------------------------------------------------------------------- #
# Round 2
# --------------------------------------------------------------------------- #
def no_progress_wiring(text: str) -> str:
    """**B5: the factory stops handing the seam a callback.**

    This is the mutation the round-1 suite could not make. Every round-1 progress test
    assigned `_on_progress(app.state)` to the *fake*, so `create_app` was never exercised and
    the argument could be anything at all -- including `None`.
    """
    return text.replace(
        "app.state.ripper_config_path, on_progress=_on_progress(app.state)",
        "app.state.ripper_config_path, on_progress=None",
    )


def partial_progress_wiring(text: str) -> str:
    """**B5, and the bug itself: round 1 used `partial` and was wrong.**

    `_on_progress(state)` *takes* the state and *returns* the callback, so
    `partial(_on_progress, state)` builds a callable that invokes
    `_on_progress(state, progress)` -- a `TypeError` on every tick, raised inside the seam's
    sampler where it becomes an unretrieved task exception. Progress never reached the store,
    the stream or the row, and nothing noticed for two rounds because the tests called the
    callback themselves. Restoring the exact line that shipped is the strongest available
    statement that the fix is load-bearing.
    """
    return text.replace(
        "app.state.ripper_config_path, on_progress=_on_progress(app.state)",
        "app.state.ripper_config_path, on_progress=partial(_on_progress, app.state)",
    )


def no_recovery(text: str) -> str:
    """B1a: a parked job has no exit, which is the round-1 state."""
    return text.replace(
        "        resumed = state.jobs.resume_waiting()\n"
        "        if resumed:\n"
        "            _log(state, f\"the wrapper is serving again; {resumed} parked job(s) requeued\")\n",
        "",
    )


def login_only_recovery(text: str) -> str:
    """B1a, in the shape the review named: recover on a *transition* only.

    Sounds tighter, and is what the unconditional version exists to defend against. A crash the
    loop never observed, a token that dies and recovers between two probes, and a hub that
    restarts holding parked jobs all have no transition to react to, and all three leave the
    job stuck for ever.
    """
    return text.replace(
        "        resumed = state.jobs.resume_waiting()\n",
        "        resumed = state.jobs.resume_waiting() if announced is not None else 0\n",
    )


def queued_only_gate(text: str) -> str:
    """B1a: the idle gate counts only `queued`, so a queue of parked jobs is never probed.

    The half of B1a that is easy to miss. The recovery lives in the branch a probe reaches;
    with this gate a queue whose only contents are `waiting` rows looks empty, never probes,
    and the recovery is unreachable -- the no-exit bug again, through the check that exists to
    avoid an HTTP call.
    """
    return text.replace(
        "    return any(True for _ in state.jobs.list(status=\"queued\")) or any(\n"
        "        True for _ in state.jobs.list(status=\"waiting\")\n"
        "    )",
        "    return any(True for _ in state.jobs.list(status=\"queued\"))",
    )


def one_park_message(text: str) -> str:
    """**B1b: one message for both reasons, saying "token" for a crash.** The round-1 state.

    Reverts `PARK_MESSAGES` to a single string used twice, so a wrapper crash tells the user
    their Apple account signed out and sends them to log in -- for a process crash that the
    supervisor's own restart budget fixes and they cannot influence.
    """
    return text.replace(
        '    "no-account": (\n'
        '        "the Apple account signed out while this was downloading: {exc} Log in from the queue "\n'
        '        "page and it resumes from here."\n'
        '    ),\n'
        '    "unavailable": (\n'
        '        "the wrapper stopped serving while this was downloading: {exc} This resumes on its own "\n'
        '        "as soon as the wrapper is back -- nothing to log in to, and no action needed unless "\n'
        '        "the wrapper does not come back."\n'
        '    ),',
        '    "no-account": "the Apple token expired while this was downloading: {exc} Log in from the "\n'
        '                  "queue page and it will resume from here.",\n'
        '    "unavailable": "the Apple token expired while this was downloading: {exc} Log in from "\n'
        '                    "the queue page and it will resume from here.",',
    )


def swap_park_messages(text: str) -> str:
    """B1b the other way: distinguishable, but the *wrong way round*.

    "Individually accurate" is a second property on top of "distinguishable", and a version
    that satisfies the first is easy to write. This attaches each reason's message to the
    other, so every park is still parked and still has a distinct string -- and tells a user to
    log in for a crash while claiming a signature is what a crash means.
    """
    collapsed = one_park_message(text)
    if collapsed == text:
        return text
    # From here the two are the same string, so distinguishing them needs one word changed.
    return collapsed.replace(
        '    "unavailable": "the Apple token expired while this was downloading: {exc} Log in from "\n'
        '                    "the queue page and it will resume from here.",',
        '    "unavailable": "the Apple account signed out while this was downloading: {exc} Log in "\n'
        '                    "from the queue page and it will resume from here.",',
    )


def allow_terminal_to_running(text: str) -> str:
    """B2: accept the transition, which is what round 1 did.

    `mark(id, "running")` on a `done` row clears `finished_at` and moves it out of the
    terminal set, at which point `DELETE` and `retry` both answer 409 and nothing will ever
    release it.
    """
    return text.replace(
        "            illegal_transition = True\n",
        "            illegal_transition = False\n",
    )


def no_transition_guard(text: str) -> str:
    """B2, the other half: the flag is computed and then not applied to the `WHERE` clause."""
    return text.replace(
        '            where += f" AND status NOT IN ({placeholders})"\n'
        "            params.extend(sorted(TERMINAL_STATUSES))\n",
        "",
    )


def never_clear_current_job(text: str) -> str:
    """B3: the id is set and never cleared, which is what round 1 did.

    Then a reading arriving after a job finished is written to whatever the variable holds --
    the *previous* job's id, on a row that already has its outcome.

    **Written as `pass`, not as a deletion.** The round-2 version removed the assignment
    outright, which left an empty `finally:` body and made the file unparseable; pytest could
    not collect it, exited non-zero, and the harness counted that as a kill. The mutation was
    vacuous evidence and the report cited it as a passing line for a full round. `pass` says
    the same thing and parses, so the kill that follows is a test failing for the right
    reason.
    """
    return text.replace(
        "        # Cleared here and not in `run_one`: `_execute` is what owns the id, so a caller that\n"
        "        # drives it directly gets the same invariant without knowing about `run_one`. The\n"
        "        # `return` in the `except` above still runs this, so the id is cleared on every path\n"
        "        # including the parked and failed ones -- which is the whole point of the check.\n"
        "        state.current_job = None\n",
        "        pass\n",
    )


def swallow_illegal_transition(text: str) -> str:
    """B2's caller half: the store refuses, and the handler lets the refusal escape.

    The refusal is in the store, so a caller that does not handle it turns a dropped reading
    into an exception on a `call_soon_threadsafe` callback -- which asyncio logs as an
    unretrieved task exception and nobody reads.
    """
    return text.replace(
        "    except IllegalTransition:\n"
        "        # The job it was describing has finished. The reading is about a transfer that is\n"
        "        # over, so dropping it loses nothing -- and the row it would have overwritten is the\n"
        "        # one carrying the real outcome.\n"
        "        return\n",
        "",
    )


# --------------------------------------------------------------------------- #
# Round 3
# --------------------------------------------------------------------------- #
def drop_the_log_sink(text: str) -> str:
    """**Item 1: the wrapper's log goes nowhere.**

    The exact line the review mutated. `log_sink=lambda line: _log(app.state, line)` is the
    production link between the supervisor's pump and the hub's event broker, and
    `FakeSupervisor` has no `log_sink` at all, so for a full round the construction block was
    never executed by a test and this mutation left every test green -- including
    `test_the_wrapper_log_reaches_the_same_stream`, which passed by calling `_log` by hand.
    """
    return text.replace(
        "            log_sink=lambda line: _log(app.state, line),",
        "            log_sink=lambda line: None,",
    )


def wrong_wrapper_settings(text: str) -> str:
    """**Item 1, second half: the block reads the wrong settings.**

    Ignores `resolved` and hardcodes, so a hub whose wrapper is on another port or behind
    another path would start a supervisor pointed at nothing. Nothing else in the app reports
    this: `/api/status` reports what the supervisor says, and the supervisor would be the one
    looking in the wrong place.
    """
    return text.replace(
        "        else WrapperSupervisor(\n"
        "            binary=resolved.wrapper_binary,\n"
        "            base_dir=resolved.wrapper_base_dir,\n"
        "            host=resolved.wrapper_host,\n"
        "            port=resolved.wrapper_port,\n",
        "        else WrapperSupervisor(\n"
        "            binary=Path('/nonexistent/lite'),\n"
        "            base_dir=Path('/nonexistent'),\n"
        "            host='127.0.0.1',\n"
        "            port=12340,\n",
    )


def probe_failure_claims_it_stopped(text: str) -> str:
    """**Item 2: a failed probe is reported as a known-down wrapper.**

    Reverts the split, so a timeout on `/status` produces "the wrapper stopped serving" -- a
    claim the evidence does not support, and the one the round-2 test could not see because it
    only checked for the *other* over-claim.
    """
    return text.replace(
        '    except Exception as exc:  # noqa: BLE001 - unknown is not ready, and the safe direction\n'
        '        return "unreachable", f"{type(exc).__name__}: {exc}"',
        '    except Exception:  # noqa: BLE001 - unknown is not ready, and the safe direction\n'
        '        return "unavailable", ""',
    )


def hedge_after_the_claim(text: str) -> str:
    """**Item 2, the subtler version: assert the fact, then correct it afterwards.**

    The first draft of the `"unreachable"` message did exactly this -- "the wrapper stopped
    serving ... and the check that says so itself failed" -- and the test caught it, because a
    sentence that opens by asserting a fact hedges it only for a reader who reads to the end.
    This is the mutation for that shape: the claim is still the first thing on the row.
    """
    collapsed = text.replace(
        '        "the check that says whether the wrapper can serve failed while this was '
        'downloading, "\n        "so it is not known what the wrapper was doing: {detail} The '
        'download itself ended "\n        "with {exc} The wrapper may well be fine; this '
        'resumes on its own as soon as the "\n        "check succeeds again."',
        '        "the wrapper stopped serving while this was downloading, and the check that '
        'says "\n        "so itself failed: {detail} This resumes on its own once the check '
        'succeeds "\n        "again; the wrapper may well be fine."',
    )
    if collapsed == text:
        return text
    return collapsed


def drop_status_filter(text: str) -> str:
    """I8: stop honouring `?status=`."""
    return text.replace(
        "        jobs = state.jobs.list(status=status, parent_url=parent)",
        "        jobs = state.jobs.list(parent_url=parent)",
    )


def drop_parent_filter(text: str) -> str:
    """I8: stop honouring `?parent=`."""
    return text.replace(
        "        jobs = state.jobs.list(status=status, parent_url=parent)",
        "        jobs = state.jobs.list(status=status)",
    )


def no_security_headers(text: str) -> str:
    """M5: send nothing."""
    return re.sub(
        r'SECURITY_HEADERS = \{.*?\n\}\n',
        "SECURITY_HEADERS: dict[str, str] = {}\n",
        text,
        flags=re.S,
    )


def no_safe_in_templates_guard(text: str) -> str:
    """M3: the guard exists but the grep is vacuous -- point it at nothing."""
    return text.replace(
        "    templates = sorted(TEMPLATES_DIR.glob(\"*.html\"))",
        "    templates = []",
    )


#: (label, path relative to `hub/`, mutation, the test file that must catch it)
CASES = [
    ("C1  reopen /openapi.json", "hub/app.py", reopen_openapi, "test_api_jobs.py"),
    ("C2  unguard the /static mount", "hub/api/__init__.py", unguarded_static, "test_api_jobs.py"),
    ("I1  ignore the session generation", "hub/auth.py", drop_generation_check, "test_api_jobs.py"),
    ("I1g ignore the generation (unit)", "hub/auth.py", drop_generation_check, "test_auth.py"),
    ("I2  key the limiter on X-Forwarded-For", "hub/api/__init__.py", peer_to_forwarded, "test_api_jobs.py"),
    ("I3  read the last XFP value", "hub/auth.py", wrong_tls_side, "test_auth.py"),
    ("I4  restore the per-call sweep", "hub/auth.py", restore_always_sweep, "test_auth.py"),
    ("I6  publish the cumulative lists", "hub/api/jobs.py", cumulative_frames, "test_api_jobs.py"),
    ("I7  fabricate is_music_video", "hub/api/jobs.py", fabricate_is_music_video, "test_api_jobs.py"),
    ("I8  drop the ?status= filter", "hub/api/jobs.py", drop_status_filter, "test_api_jobs.py"),
    ("I8b drop the ?parent= filter", "hub/api/jobs.py", drop_parent_filter, "test_api_jobs.py"),
    ("C4  fail instead of parking", "hub/app.py", fail_instead_of_park, "test_api_jobs.py"),
    ("C4b park everything", "hub/app.py", always_park, "test_api_jobs.py"),
    ("PF  progress written, not published", "hub/app.py", drop_progress_publish, "test_api_jobs.py"),
    ("PFb progress published, not written", "hub/app.py", no_progress_mark, "test_api_jobs.py"),
    ("I5  probe the wrapper while idle", "hub/app.py", slow_idle_poll, "test_api_jobs.py"),
    ("M5  send no security headers", "hub/api/__init__.py", no_security_headers, "test_api_jobs.py"),
    ("M3  vacuous template grep", "tests/test_api_jobs.py", no_safe_in_templates_guard, "test_api_jobs.py"),
    ("R1  trust rip_song's return value", "hub/ripper_host.py", trust_a_task_that_says_nothing, "test_ripper_host.py"),
    ("R1b the sink keeps a stale task", "hub/ripper_host.py", sink_reports_the_previous_task, "test_ripper_host.py"),
    ("I5c cache the readiness answer", "hub/app.py", idle_poll_cached, "test_api_jobs.py"),
    # -- round 2
    ("B5  create_app passes no callback", "hub/app.py", no_progress_wiring, "test_api_jobs.py"),
    ("B5b create_app passes a partial", "hub/app.py", partial_progress_wiring, "test_api_jobs.py"),
    ("B1a a parked job has no exit", "hub/app.py", no_recovery, "test_api_jobs.py"),
    ("B1a2 recover on a transition only", "hub/app.py", login_only_recovery, "test_api_jobs.py"),
    ("B1a3 the gate counts only queued", "hub/app.py", queued_only_gate, "test_api_jobs.py"),
    ("B1b one message for both reasons", "hub/app.py", one_park_message, "test_api_jobs.py"),
    ("B1b2 the messages are swapped", "hub/app.py", swap_park_messages, "test_api_jobs.py"),
    ("B3  current_job is never cleared", "hub/app.py", never_clear_current_job, "test_api_jobs.py"),
    ("B2  allow terminal -> running", "hub/jobs.py", allow_terminal_to_running, "test_jobs.py"),
    ("B2b the guard is not in the SQL", "hub/jobs.py", no_transition_guard, "test_jobs.py"),
    ("B2c the caller does not handle it", "hub/app.py", swallow_illegal_transition, "test_api_jobs.py"),
    # -- round 3
    ("1   the wrapper's log goes nowhere", "hub/app.py", drop_the_log_sink, "test_api_jobs.py"),
    ("1b  the block ignores the settings", "hub/app.py", wrong_wrapper_settings, "test_api_jobs.py"),
    ("2   a failed probe claims it stopped", "hub/app.py", probe_failure_claims_it_stopped, "test_api_jobs.py"),
    ("2b  claim first, hedge after", "hub/app.py", hedge_after_the_claim, "test_api_jobs.py"),
]


def _run_suite(path: Path) -> tuple[bool, str]:
    """Run pytest and say whether it stayed green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(path), "-q", "-x", "-p", "no:cacheprovider"],
        cwd=HUB,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode == 0, (result.stdout + result.stderr)[-600:]


def _parses(source: str) -> str:
    """`""` if `source` is valid Python, else the `SyntaxError` as one line.

    **This exists because a `SyntaxError` was being counted as a kill.** The round-2 `B3`
    mutation deleted a statement from a `finally:` body, which left the block empty; pytest
    could not even *collect* the file, exited non-zero, and the harness reported "killed".
    That is not evidence that any test noticed anything -- it is evidence that the file no
    longer parses, and the report cited it as a passing mutation for a full round.

    So every mutation is parsed before it is written. One that does not parse is reported as
    **INVALID** and counted as a failure of the harness, not as a kill: it means the patch
    string no longer matches the code it was written against, which is a stale mutation rather
    than a surviving one. Both are worth stopping for, and conflating them is what made the
    B3 line meaningless.
    """
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno})"
    return ""


def _dirty_paths() -> list[Path]:
    """Every source file whose content differs from what was read before the run.

    Checked with hashes captured up front rather than with `git`, because the working tree is
    *expected* to be dirty -- round 1's own changes are uncommitted -- and a `git stash`-based
    check would either pass vacuously or throw away work. This compares against what this run
    read, which is exactly the question.
    """
    dirty = []
    for relative in sorted({case[1] for case in CASES}):
        path = HUB / relative
        if _digest(path) != BASELINE.get(relative):
            dirty.append(path)
    return dirty


def _digest(path: Path) -> str:
    return hashlib.sha256(_read(path).encode("utf-8")).hexdigest()


def main() -> int:
    # `--check-guard` runs the parse guard on its own and exits. It exists so that a *spike*
    # script can prove the guard rejects an unparseable mutation without importing this
    # module: `tests/test_ripper_host.py`'s boundary refuses `importlib` in a `spike/` file
    # (correctly -- it is the "no loader tricks" rule), and shelling out to a documented entry
    # point is both allowed and more honest, because it exercises the argument parsing and the
    # canary as a caller would rather than reaching past them.
    if "--check-guard" in sys.argv[1:]:
        return _guard_only()
    return _run_all()


def _guard_only() -> int:
    """Exercise `_parses` on three known sources and report.

    Exit 0 has proved: an unbalanced `def f(:` is rejected, an empty `finally:` is rejected --
    **the exact shape of the round-2 B3 mutation** -- and ordinary valid code is accepted. A
    non-zero exit means the guard is broken, and with it every "killed" line this harness has
    ever printed.
    """
    cases = (
        ("unbalanced paren", "def f(:\n    pass\n", False),
        ("empty finally", "try:\n    pass\nfinally:\n", False),
        ("valid code", "def f():\n    return 1\n", True),
    )
    for label, source, should_parse in cases:
        problem = _parses(source)
        got = not problem
        print(f"  {label:20} {'accepted' if got else 'rejected: ' + problem}")
        if got != should_parse:
            print(f"FAIL: {label} -- expected {'accepted' if should_parse else 'rejected'}")
            return 1
    print("OK: the parse guard accepts valid code and rejects both unparseable shapes.")
    return 0


def _run_all() -> int:
    global BASELINE  # noqa: PLW0603 - a script-level snapshot, set once at start-up
    BASELINE = {relative: _digest(HUB / relative) for _, relative, _, _ in CASES}
    for relative, expected in BASELINE.items():
        if _digest(HUB / relative) is None or not (HUB / relative).is_file():
            print(f"FAIL: {relative} does not exist; this script's patches are stale")
            return 1

    # The guard above is only worth having if it is exercised, and a guard nobody runs is a
    # comment. So one canary runs on every invocation: a mutation that is *guaranteed* not to
    # parse, which must be reported INVALID. Before `_parses` existed it would have been
    # written to disk and counted as a kill, which is exactly the mistake the round-2 report
    # made.
    canary = "def _canary(:\n    pass\n"
    if not _parses(canary):
        print("FAIL: the parse guard accepted a file with a syntax error; every 'killed' "
              "line below is suspect")
        return 1
    print("  canary               the parse guard rejects an unparseable mutation, as intended")

    survivors: list[str] = []
    invalid: list[str] = []
    checked = 0
    for label, relative, mutation, test_file in CASES:
        target = HUB / relative
        original = _read(target)
        mutated = mutation(original)
        if mutated == original:
            print(f"SKIP {label}: the mutation did not apply ({target.name})")
            survivors.append(f"{label} (mutation did not apply)")
            continue

        # A mutation that does not parse is *not* a kill. See `_parses`: pytest would exit
        # non-zero on a collection error, which is indistinguishable from a test failing, and
        # the round-2 `B3` line of the report is what that mistake cost.
        problem = _parses(mutated)
        if problem:
            print(f"INVALID {label}: the mutation does not parse -- {problem}. The patch "
                  f"string is stale, not the test weak.")
            invalid.append(f"{label} (does not parse: {problem})")
            continue

        _write(target, mutated)
        try:
            green, output = _run_suite(HUB / "tests" / test_file)
        finally:
            _write(target, original)

        checked += 1
        if green:
            survivors.append(label)
            print(f"SURVIVED {label}  <-- {test_file} stayed green")
        else:
            first = next(
                (line for line in output.splitlines() if line.startswith("FAILED")
                 or "assert" in line.lower()),
                "",
            )
            print(f"  killed  {label:44} by {test_file}: {first.strip()[:90]}")

    # The repository is byte-identical to how it was found. The script edits real files, so
    # this is the assertion that matters most: a mutation that failed to restore would leave
    # the working tree in a state where the next `pytest` run is testing something else.
    print()
    dirty = [path.relative_to(HUB) for path in _dirty_paths()]
    if dirty:
        print(f"the working tree was not restored: {dirty}")
        return 1

    print(f"{checked} mutations applied, {checked - len(survivors)} killed, tree restored.")
    if survivors:
        for name in survivors:
            print(f"SURVIVED: {name}")
    if invalid:
        for name in invalid:
            print(f"INVALID: {name}")
    if survivors or invalid:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
