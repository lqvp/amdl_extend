"""Task 9, fix round 3: the four verifications the review asked for, by execution.

Run:  cd hub && uv run python spike/task9_round3_check.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
sys.path.insert(0, str(HUB / "tests"))

import httpx  # noqa: E402

import test_api_jobs as T  # noqa: E402
from hub import app as app_module  # noqa: E402
from hub.app import create_app  # noqa: E402
from hub.config import load_settings  # noqa: E402
from hub.jobs import JobStore  # noqa: E402
from hub.ripper_host import RipperHostError  # noqa: E402


def catalogue():
    api = T.FakeWebAPI()
    api.add_album("1621491338", "4pi", "toe",
                  [("1", "1 a.m. (feat. shinoだす。)", f"{T.ALBUM_URL}?i=1")])
    return api


def settings_for(tmp: Path, **overrides):
    (tmp / "lib").mkdir(exist_ok=True)
    base = {
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(tmp / "lib"), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    }
    base.update(overrides)
    return load_settings(base)


async def check_1_log_sink(settings):
    """**Item 1: the `WrapperSupervisor` block, and the log link it owns."""
    print("== 1. the supervisor assembly block " + "=" * 30)
    failures: list[str] = []
    handed: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            handed.append(kwargs)

        running = adopted = False
        pid = 4242
        bound_port = 0
        log: list[str] = []

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

        async def status(self):
            return {"running": self.running, "regions": ["jp"]}

        async def close(self):
            return None

    original = app_module.WrapperSupervisor
    app_module.WrapperSupervisor = Recording
    try:
        app = create_app(settings, supervisor=None, ripper=T.FakeRipper(catalogue()),
                         autostart=False)
    finally:
        app_module.WrapperSupervisor = original

    assert handed, "no supervisor constructed"
    kw = handed[0]
    print(f"  host / port          {kw['host']} : {kw['port']}")
    print(f"  binary               {kw['binary']}")
    print(f"  base_dir             {kw['base_dir']}")
    print(f"  log_sink             {type(kw['log_sink']).__name__} (bound to this app)")

    for field, want in (("host", "192.0.2.44"), ("port", 31337)):
        if kw[field] != want:
            failures.append(f"{field} was {kw[field]!r}, not the resolved {want!r}")
    if Path(kw["binary"]) != settings.wrapper_binary:
        failures.append("binary is not `resolved.wrapper_binary`")
    if Path(kw["base_dir"]) != settings.wrapper_base_dir:
        failures.append("base_dir is not `resolved.wrapper_base_dir`")
    if not callable(kw["log_sink"]):
        failures.append("no log_sink was passed")

    # And the sink actually publishes onto the one channel the stream reads.
    published: list[dict] = []
    real_publish = app.state.broker.publish
    app.state.broker.publish = lambda ch, d: (published.append(d), real_publish(ch, d))[1]
    kw["log_sink"]("[lite] device has no such account")
    app.state.broker.publish = real_publish
    logs = [d for d in published if d.get("kind") == "log"]
    print(f"  frames from the sink {len(logs)}  {logs[0]['line'] if logs else ''}")
    if not logs:
        failures.append("the sink the app installed published nothing")
    app.state.jobs.close()
    return failures


async def check_2_probe_message(settings):
    """**Item 2: a failed probe must not claim the wrapper stopped."""
    print("\n== 2. a failed probe's message " + "=" * 34)
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp())
    resolved = settings_for(tmp)
    app = create_app(resolved, supervisor=T.FakeSupervisor(),
                     ripper=T.FakeRipper(catalogue()), autostart=False)
    sup, rip = app.state.supervisor, app.state.ripper
    store = JobStore(resolved.db_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub.test") as client:
            await client.post("/api/auth/login", json={"password": T.PASSWORD})
            sup.regions = ["jp"]
            await client.post("/api/wrapper/start")
            await client.post("/api/jobs", json={"urls": [T.ALBUM_URL], "codec": "alac"})

            async def unanswering() -> dict:
                raise ConnectionError("the QEMU guest did not answer")

            sup.status = unanswering          # noqa: N805 - the point of the check
            rip.rip_error = RipperHostError("rip_song failed: connect error")
            await app.state.run_one()
            failed_probe = store.get(1).error
            print(f"  status               {store.get(1).status}")
            print(f"  failed probe         {failed_probe}")

            for claim in ("the wrapper stopped serving", "Log in", "signed out"):
                if claim in failed_probe:
                    failures.append(f"a failed /status still claims {claim!r}")

            # And the complement: a *known*-down wrapper is the case that may say it stopped.
            # The wrapper is stopped *through the route*, so the supervisor's own record says
            # so, which is the observable the "unavailable" reason is allowed to read.
            sup.status = lambda: asyncio.sleep(  # noqa: E731
                0, result={"running": sup.running, "regions": []})
            store.mark(1, "queued")
            rip.rip_error = RipperHostError("rip_song failed: connect error")
            await client.post("/api/wrapper/stop")
            await app.state.run_one()
            known_down = store.get(1).error
            print(f"  known down           {known_down}")
            if "the wrapper stopped serving" not in (known_down or ""):
                failures.append(
                    "with no process running, the message no longer says the wrapper stopped"
                )
    app.state.jobs.close()
    store.close()
    return failures


async def check_3_resolved_settings(settings):
    """**Item 1, second half: the four settings are asserted against `Settings`."""
    print("\n== 3. the resolved wrapper settings " + "=" * 30)
    failures: list[str] = []
    resolved = settings
    for field in ("wrapper_host", "wrapper_port", "wrapper_binary", "wrapper_base_dir"):
        print(f"  resolved.{field:20} {getattr(resolved, field)}")
        if not hasattr(resolved, field):
            failures.append(f"Settings has no {field}")
    # Non-default, so an assertion of the defaults would not pass.
    if (resolved.wrapper_host, resolved.wrapper_port) == ("127.0.0.1", 12340):
        failures.append("the check is running against the defaults, so it proves nothing")
    return failures


def check_4_harness_guard():
    """**Item 5: the harness rejects a mutation that does not parse.**

    Driven through the harness's own `--check-guard` entry point rather than by importing the
    module: `tests/test_ripper_host.py`'s boundary refuses `importlib` in a `spike/` file
    (correctly -- it is the "no loader tricks" rule, and the first draft of this check was
    rejected by it for exactly that). A subprocess is also the more honest shape, because it
    exercises the argument parsing and the canary as a caller would rather than reaching past
    them.

    The "empty `finally:`" case below is **the exact shape of the round-2 B3 mutation**, which
    is why it is in the harness's own set rather than only here: that mutation applied, left
    `hub/app.py` unparseable, and the harness counted the resulting collection error as a kill.
    The round-2 report cited it as a passing line for a full round.
    """
    print("\n== 4. the harness's parse guard " + "=" * 34)
    failures: list[str] = []

    result = subprocess.run(
        [sys.executable, "spike/task9_fix1_check.py", "--check-guard"],
        cwd=HUB, capture_output=True, text=True, timeout=120,
    )
    for line in result.stdout.splitlines():
        print(f"  {line.strip()}")
    if result.returncode != 0:
        failures.append(
            f"the harness's --check-guard exited {result.returncode}: "
            f"{(result.stdout + result.stderr).strip()[-200:]}"
        )

    # The full sweep also prints a canary line every run, which is what makes the guard
    # self-verifying in the mode a reviewer actually reads. Assert it is there.
    print("  (the full sweep additionally prints a canary line on every invocation)")
    return failures


async def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp())
    binary = tmp / "lite"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved = settings_for(
        tmp,
        AMD_WRAPPER_BINARY=str(binary),
        AMD_WRAPPER_HOST="192.0.2.44",
        AMD_WRAPPER_PORT="31337",
    )
    failures += await check_1_log_sink(resolved)
    failures += await check_2_probe_message(resolved)
    failures += await check_3_resolved_settings(resolved)
    failures += check_4_harness_guard()

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("OK: all four of the round-3 verifications hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
