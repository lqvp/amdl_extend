"""Task 9, fix round 2: the four verifications the review asked for, by execution.

Run:  cd hub && uv run python spike/task9_round2_check.py
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import httpx  # noqa: E402

import test_api_jobs as T  # noqa: E402
from hub import app as app_module  # noqa: E402
from hub.app import create_app  # noqa: E402
from hub.config import load_settings  # noqa: E402
from hub.jobs import IllegalTransition, JobStore, Progress  # noqa: E402
from hub.ripper_host import RipperHostError  # noqa: E402


def settings_for(tmp: Path):
    lib = tmp / "lib"
    lib.mkdir()
    return load_settings({
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(lib), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    })


def catalogue():
    api = T.FakeWebAPI()
    api.add_album("1621491338", "4pi", "toe",
                  [("1", "1 a.m. (feat. shinoだす。)", f"{T.ALBUM_URL}?i=1")])
    return api


def build(settings, ripper=None):
    """The app under test. `ripper=None` is deliberate everywhere it is passed: it is the
    branch `create_app` takes when nothing is injected, and it is the branch that constructs
    the seam -- the one that hands over `on_progress` and the one no round-1 test took."""
    kwargs = {} if ripper is None else {"ripper": ripper}
    return create_app(settings, supervisor=T.FakeSupervisor(), autostart=False, **kwargs)


async def wait_until(predicate, timeout=5.0, interval=0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- #
async def check_1_on_progress_is_live():
    """**B5: `create_app` hands the seam a callback that works.**

    The real factory, no injected ripper, only the class replaced -- so this is the same
    branch a user's hub takes.
    """
    print("== 1. create_app's progress wiring " + "=" * 27)
    failures = []
    handed: list = []

    class RecordingHost:
        started = closed = False

        def __init__(self, config_path, *, on_progress=None):
            handed.append(on_progress)

        async def start(self):
            self.started = True

        async def close(self):
            self.closed = True

    tmp = Path(tempfile.mkdtemp())
    original = app_module.RipperHost
    app_module.RipperHost = RecordingHost
    try:
        # `ripper=None` is the point: this is the branch that constructs the seam, and it is
        # the branch no round-1 test ever took.
        app = build(settings_for(tmp), ripper=None)
    finally:
        app_module.RipperHost = original

    print(f"  seam constructed       {type(app.state.ripper).__name__}")
    print(f"  on_progress handed     "
          f"{'None' if not handed or handed[0] is None else 'a callable'}")
    if not handed or handed[0] is None:
        failures.append("create_app built the seam with on_progress=None")

    store = JobStore(app.state.settings.db_path)
    store.create_batch(T.ALBUM_URL, "album",
                       [T.Leaf(adam_id="1", title="t", album_name="A", artist_name="X",
                               codec="alac", language="ja", url=T.ALBUM_URL,
                               storefront="jp")], force=False)
    store.mark(1, "running")
    published: list = []
    real_publish = app.state.broker.publish
    app.state.broker.publish = lambda ch, d: (published.append(d), real_publish(ch, d))[1]
    # Inside the lifespan, because the callback needs the loop it defers to and the loop is
    # captured there. `autostart=False`, so this starts no wrapper and no scheduler -- the
    # lifespan's only job here is to set `state.loop`, which is the third thing the wiring
    # depends on and the one a unit test of the callback would also have provided by hand.
    async with app.router.lifespan_context(app):
        app.state.current_job = 1
        handed[0](Progress(bytes_done=300, bytes_total=1000, fraction=0.3))
        app.state.current_job = None
        await asyncio.sleep(0)
    job = store.get(1)
    print(f"  row after the reading  bytes_done={job.bytes_done} "
          f"progress={job.progress} bytes_total={job.bytes_total}")
    if job.bytes_done != 300 or job.progress is None:
        failures.append("the callback the factory installed wrote nothing to the store")
    frames = [f for f in published if f.get("kind") == "job"]
    print(f"  job frames published   {len(frames)}")
    if not frames:
        failures.append("the reading reached the store but nothing was published")
    app.state.broker.publish = real_publish
    app.state.jobs.close()
    store.close()
    return failures


async def check_2_crash_recovers(settings):
    """**B1a: the wrapper goes down, comes back, and the parked job runs. No login."""
    print("\n== 2. a crash-parked job is released with no login " + "=" * 17)
    failures = []
    tmp = Path(tempfile.mkdtemp())
    settings = load_settings({
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(tmp / "lib"), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    })
    (tmp / "lib").mkdir()
    app = build(settings, ripper=T.FakeRipper(catalogue()))
    sup, rip = app.state.supervisor, app.state.ripper
    store = JobStore(settings.db_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub.test") as client:
            await client.post("/api/auth/login", json={"password": T.PASSWORD})
            sup.regions = ["jp"]
            await client.post("/api/wrapper/start")
            await client.post("/api/jobs", json={"urls": [T.ALBUM_URL], "codec": "alac"})

            # The wrapper dies mid-rip.
            await client.post("/api/wrapper/stop")
            rip.rip_error = RipperHostError("rip_song failed: connect error")
            await app.state.run_one()
            print(f"  after the crash        {store.get(1).status}")
            print(f"  message                {store.get(1).error[:72]}...")
            if store.get(1).status != "waiting":
                failures.append(f"a crash parked the job as {store.get(1).status!r}")

            # It comes back by itself. No login, no 2FA, no retry.
            print(f"  logins performed       "
                  f"{len([c for c in sup.log if c.startswith('login')])}")
            sup._running = True
            sup.regions = ["jp"]
            rip.rip_error = None
            task = asyncio.create_task(app_module.scheduler_loop(app.state))
            try:
                recovered = await wait_until(lambda: store.get(1).status == "done")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            print(f"  after it came back     {store.get(1).status}")
            if not recovered:
                failures.append("the job is still parked after the wrapper recovered")
    app.state.jobs.close()
    store.close()
    return failures


async def check_3_terminal_to_running(settings):
    """**B2: the store refuses, and the row is untouched."""
    print("\n== 3. a finished job cannot become running " + "=" * 28)
    failures = []
    tmp = Path(tempfile.mkdtemp())
    (tmp / "lib").mkdir()
    app = build(load_settings({
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(tmp / "lib"), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    }), ripper=T.FakeRipper(catalogue()))
    store = JobStore(app.state.settings.db_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub.test") as client:
            await client.post("/api/auth/login", json={"password": T.PASSWORD})
            await client.post("/api/jobs", json={"urls": [T.ALBUM_URL], "codec": "alac"})
            await app.state.run_one()
            done = store.get(1)
            print(f"  before                 {done.status}  finished_at set: "
                  f"{done.finished_at is not None}")

            # The late reading, through the real handler.
            from hub import app as A
            app.state.current_job = 1
            A._on_progress(app.state)(Progress(bytes_done=5, bytes_total=10, fraction=0.5))
            app.state.current_job = None
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            after = store.get(1)
            print(f"  after a late reading   {after.status}  finished_at set: "
                  f"{after.finished_at is not None}")
            if after.status != "done" or after.finished_at is None:
                failures.append(f"the row became {after.status!r} with "
                                f"finished_at={after.finished_at!r}")

            # And the user is not locked out of it.
            retry = await client.post("/api/jobs/1/retry")
            delete = await client.delete("/api/jobs/1")
            print(f"  retry / delete         {retry.status_code} / {delete.status_code}")
            if retry.status_code != 200 or delete.status_code != 200:
                failures.append("a finished job can no longer be retried or deleted")

            # The refusal itself, with the exception named.
            store.mark(1, "done")
            try:
                store.mark(1, "running")
                failures.append("mark(1, 'running') on a done row was accepted")
            except IllegalTransition as exc:
                print(f"  the refusal            IllegalTransition: {str(exc)[:64]}...")
    app.state.jobs.close()
    store.close()
    return failures


async def check_4_two_park_messages(settings):
    """**B1b: the two reasons say different, individually true things."""
    print("\n== 4. the two park reasons " + "=" * 40)
    failures = []
    tmp = Path(tempfile.mkdtemp())
    (tmp / "lib").mkdir()
    app = build(load_settings({
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(tmp / "lib"), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    }), ripper=T.FakeRipper(catalogue()))
    sup, rip = app.state.supervisor, app.state.ripper
    store = JobStore(app.state.settings.db_path)
    seen: dict[str, str] = {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub.test") as client:
            await client.post("/api/auth/login", json={"password": T.PASSWORD})
            sup.regions = ["jp"]
            await client.post("/api/wrapper/start")
            await client.post("/api/jobs", json={"urls": [T.ALBUM_URL], "codec": "alac"})

            # reason 1: the wrapper is not running
            await client.post("/api/wrapper/stop")
            rip.rip_error = RipperHostError("rip_song failed: connect error")
            await app.state.run_one()
            seen["wrapper down"] = store.get(1).error

            # reason 2: serving, with no account
            store.mark(1, "queued")
            sup._running = True
            sup.regions = []
            rip.rip_error = RipperHostError("rip_song failed for adam_id=1: no such account")
            await app.state.run_one()
            seen["account signed out"] = store.get(1).error

    for reason, message in seen.items():
        print(f"  {reason:20} {message[:88]}")
    down, out = seen["wrapper down"], seen["account signed out"]
    if "Log in" in down:
        failures.append("a crashed wrapper is told to log in")
    if "Log in" not in out:
        failures.append("a signed-out account is not told to log in")
    if down == out:
        failures.append("the two reasons produce the same message")
    app.state.jobs.close()
    store.close()
    return failures


async def main() -> int:
    failures: list[str] = []
    failures += await check_1_on_progress_is_live()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "lib").mkdir()
    settings = load_settings({
        "AMD_PASSWORD": T.PASSWORD, "AMD_SESSION_SECRET": T.SECRET,
        "AMD_LIBRARY_ROOTS": str(tmp / "lib"), "AMD_DB_PATH": str(tmp / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(tmp / "wrapper"),
    })
    failures += await check_2_crash_recovers(settings)
    failures += await check_3_terminal_to_running(settings)
    failures += await check_4_two_park_messages(settings)

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("OK: all four of the round-2 verifications hold against the real app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
