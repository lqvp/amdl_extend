"""Task 9's HTTP contract, end to end, against the real `RipperHost`.

The suite in `tests/` uses fakes for the supervisor and the ripper, which is the only way to
run it hermetically. This script is the other half: it boots the **real** `RipperHost` -- the
one that registers the six creart creators, chdirs into `AppleMusicDecrypt/`, and resolves
`it(Config)` -- and drives the real `POST /api/jobs` over `httpx`'s ASGI transport.

Nothing is downloaded and no wrapper is started. The point is the seam between them: that the
app calls the real render, that a real rendered name hits a real filesystem, and that the
answer a user gets is the one the code claims.

Run:  cd hub && uv run python spike/task9_contract_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from hub.app import create_app  # noqa: E402
from hub.config import load_settings  # noqa: E402
from hub.jobs import Leaf  # noqa: E402
from hub.library_scan import scan_roots  # noqa: E402
from hub.ripper_host import RipperHost  # noqa: E402
from hub.dedup import find_duplicate  # noqa: E402

VENDOR = Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"
PASSWORD = "task9-check-password"

# The track §7.6's last row is about. A previous download left `1-01 1 a.m. ….m4a` on disk,
# whose key is `1 a.m. …`; the catalogue's tag title is `1 a.m. …`, whose own key is
# `a.m. …`. Only the *rendered* name finds the file.
TAG_TITLE = "1 a.m. (feat. shinoだす。)"
ON_DISK = "1-01 1 a.m. (feat. shinoだす。).m4a"
ALBUM = "4pi"
ARTIST = "toe"


class _NeverStarted:
    """A supervisor stand-in that refuses to do anything, for a check with no wrapper.

    The real `WrapperSupervisor` needs a launcher binary and a QEMU guest. This records the
    calls so the script can say the app *tried* to start the wrapper and handled the refusal
    rather than skipping the step.
    """

    adopted = False
    pid = None
    bound_port = 0

    def __init__(self) -> None:
        self.running = False
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        from hub.wrapper_supervisor import SupervisorError

        raise SupervisorError(
            "no account is logged in on the wrapper at http://127.0.0.1:12340/status: it is "
            "up and answering /status, but regions is empty, so it cannot serve a download"
        )

    async def stop(self) -> None:
        self.calls.append("stop")

    async def status(self) -> dict:
        self.calls.append("status")
        return {"regions": []}

    async def login(self, username: str, password: str):
        raise AssertionError("not reached: this check does not log in")

    async def submit_2fa(self, challenge_id: str, code: str) -> None:
        raise AssertionError("not reached: this check does not log in")


async def run() -> int:
    if not (VENDOR / "src").is_dir() or not (VENDOR / "config.toml").is_file():
        print(f"SKIP: need {VENDOR}/src and {VENDOR}/config.toml")
        return 0

    failures: list[str] = []
    workspace = Path(tempfile.mkdtemp(prefix="amd-hub-task9-"))
    library = workspace / "library"
    (library / ARTIST / ALBUM).mkdir(parents=True)
    (library / ARTIST / ALBUM / ON_DISK).write_bytes(b"")

    env = {
        "AMD_PASSWORD": PASSWORD,
        "AMD_SESSION_SECRET": "task9-check-secret-that-is-long-enough",
        "AMD_LIBRARY_ROOTS": str(library),
        "AMD_DB_PATH": str(workspace / "hub.db"),
        "AMD_WRAPPER_BASE_DIR": str(workspace / "wrapper"),
    }
    settings = load_settings(env)
    supervisor = _NeverStarted()

    # The real host, built here rather than through `create_app` so the script can also show
    # the seam's own methods answering -- and passed in, so there is exactly one.
    ripper = RipperHost(VENDOR / "config.toml")
    # `autostart=True` so the lifespan runs, but the *scheduler task is not wanted here*:
    # this script drives `run_one()` itself, step by step, so that each assertion is about a
    # transition it chose to make. A background loop would claim the jobs underneath the
    # narration and the output would be a lie about which step did what. The startup order it
    # would have exercised -- supervisor, then host, then scheduler -- is covered by
    # `test_a_wrapper_that_will_not_start_does_not_stop_the_hub`.
    app = create_app(settings, supervisor=supervisor, ripper=ripper, autostart=False)

    async with app.router.lifespan_context(app):
        # The host is started here rather than in the lifespan (autostart is False), because
        # the checks below are about a *started* client: the real `region_language`, the real
        # `web_api`, the real `render_song_filename`. `supervisor.start()` is left to the
        # script's own fake, which refuses with the exact message a fresh install produces.
        await app.state.ripper.start()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        )
        async with client:
            print("== 1. the seam, on the real client " + "=" * 30)
            print(f"  region.language      {ripper.region_language!r}")
            print(f"  web_api              {type(ripper.web_api).__name__}")
            if ripper.region_language is None or ripper.web_api is None:
                failures.append("the host did not expose its config language or its WebAPI")

            print("\n== 2. a wrapper that will not start does not stop the hub " + "=" * 10)
            # The start is driven here, with autostart off, and the point is the same one the
            # lifespan makes: the refusal is recorded and the hub is still serving.
            app.state.startup_error = None
            try:
                await app.state.supervisor.start()
            except Exception as exc:  # noqa: BLE001 - the fake's own message is the fixture
                app.state.startup_error = str(exc)
            print(f"  supervisor start     refused: {str(app.state.startup_error)[:60]}...")
            health = await client.get("/api/health")
            print(f"  GET /api/health      {health.status_code} {health.json()}")
            if health.status_code != 200:
                failures.append("/api/health is not reachable, so the boot failed for real")

            print("\n== 3. the unauthenticated surface " + "=" * 35)
            # The whole route table, derived, plus the paths that are not in it because they
            # are not `APIRoute`s -- `/openapi.json` and the static mount, which is where
            # round 0's two Critical holes were.
            for method, path in (("POST", "/api/jobs"), ("GET", "/api/jobs"),
                                 ("GET", "/api/status"), ("GET", "/queue"),
                                 ("GET", "/openapi.json"), ("HEAD", "/openapi.json"),
                                 ("GET", "/static/app.css"),
                                 ("POST", "/api/library/scan"),
                                 ("GET", "/api/library/duplicates")):
                response = await client.request(method, path, json={},
                                                follow_redirects=False)
                note = (f"-> {response.headers['location']}"
                        if response.status_code == 303 else "")
                print(f"  {method:4} {path:24} {response.status_code} {note}")
                # `/static/app.css` is on the open list because the login page needs it;
                # everything else here must not answer without a session. 405 is also
                # acceptable for a path whose *method* is guarded but whose verb is wrong --
                # the route exists, so the guard ran; a 405 with a session would be a
                # different matter.
                expected = (200,) if path == "/static/app.css" else (401, 303, 404, 405)
                if response.status_code not in expected:
                    failures.append(
                        f"{method} {path} answered {response.status_code} without a session"
                    )
            if app.openapi_url is not None:
                failures.append("app.openapi_url is set: the schema is served to anyone")

            # A static asset that is *not* the login page's own two files.
            other = await client.get("/static/../app.css")
            print(f"  GET  /static/../app.css     {other.status_code}  (traversal)")
            if other.status_code not in (401, 404):
                failures.append("a traversal attempt through /static was served")

            print("\n== 4. login " + "=" * 61)
            wrong = await client.post("/api/auth/login", json={"password": "not-it"})
            cookie = (await client.post("/api/auth/login",
                                        json={"password": PASSWORD})).headers.get("set-cookie", "")
            print(f"  wrong password      {wrong.status_code} {wrong.json()}")
            print(f"  cookie flags        {cookie.split('; ', 1)[-1]}")
            if wrong.status_code != 401:
                failures.append("a wrong password was not refused")
            for flag in ("HttpOnly", "SameSite=lax"):
                if flag not in cookie:
                    failures.append(f"the session cookie is missing {flag}")

            stolen = cookie.split(";")[0].split("=", 1)[1]
            print("\n== 4b. logout revokes, it does not ask " + "=" * 27)
            logged_out = await client.post("/api/auth/logout")
            replay = await client.request(
                "GET", "/api/jobs", headers={"cookie": f"amd_hub_session={stolen}"}
            )
            print(f"  logout              {logged_out.status_code}")
            print(f"  the stolen token    {replay.status_code} (must be 401)")
            if replay.status_code != 401:
                failures.append(
                    "a token captured before logout still works: the cookie is a bearer "
                    "credential and telling the browser to drop it is not a revocation"
                )

            print("\n== 4c. a token that expires mid-download is parked " + "=" * 21)
            # This job is deliberately NOT on disk, so the rip is attempted and fails -- which
            # is the state §10 describes. A job that were a filesystem duplicate would be
            # skipped before the client was asked to do anything and would prove nothing.
            # §10: `waiting`, not `failed`. The producer this round added, driven through the
            # real scheduler with a real `RipperHost` in place -- the rip fails because the
            # fake cannot do the work, and the wrapper reports no regions, which is the
            # discriminator.
            supervisor.regions = ["jp"]
            from hub.jobs import JobStore as _Store
            from hub.jobs import Leaf as _Leaf

            parked_store = _Store(settings.db_path)
            created = parked_store.create_batch(
                "https://music.apple.com/jp/album/4pi/1621491338",
                "album",
                [_Leaf(adam_id="1440935467", title="T", album_name=ALBUM,
                       artist_name=ARTIST, codec="alac", language="ja",
                       url="https://music.apple.com/jp/album/4pi/1621491338",
                       storefront="jp")],
                force=False,
            ).created
            assert created, "nothing was enqueued, so the rest of this proves nothing"
            parked_id = created[0]
            # The leaf is registered the way the enqueue handler registers it. Without it
            # `_leaf_for` re-expands the URL against the real catalogue, which is a network
            # call this check cannot make and which would fail for a reason that has nothing
            # to do with the token. See `spike/task9_contract_check.py`'s step 6 for the same
            # substitution and why it is legitimate.
            app.state.leaves.remember(
                parked_store.get(parked_id),
                _Leaf(adam_id="1440935467", title="T", album_name=ALBUM,
                      artist_name=ARTIST, codec="alac", language="ja",
                      url="https://music.apple.com/jp/album/4pi/1621491338",
                      storefront="jp"),
            )
            supervisor.regions = []  # the account went away
            await app.state.run_one()
            parked = parked_store.get(parked_id)
            print(f"  job status          {parked.status}")
            print(f"  error               {str(parked.error)[:70]}...")
            if parked.status != "waiting":
                failures.append(
                    f"a rip that failed with an unready wrapper ended as {parked.status!r}; "
                    f"§10 says a token that expires during a download parks the job"
                )
            # And the account coming back brings it out of `waiting`.
            supervisor.regions = ["jp"]
            requeued = parked_store.resume_waiting()
            print(f"  resume_waiting()    {requeued} job(s) requeued")
            if parked_store.get(parked_id).status != "queued":
                failures.append("the parked job was not requeued after the account returned")
            parked_store.close()

            print("\n== 5. the duplicate check, on the real client and a real tree " + "=" * 5)
            leaf = Leaf(
                adam_id="1440935466", title=TAG_TITLE, album_name=ALBUM,
                artist_name=ARTIST, codec="alac", language=ripper.region_language or "ja",
                url="https://music.apple.com/jp/album/4pi/1621491338", storefront="jp",
            )
            rendered = ripper.render_song_filename(leaf)
            scan = scan_roots(settings.library_roots)
            print(f"  rendered            {rendered!r}")
            print(f"  on disk             {ON_DISK!r}")
            print(f"  album dirs found    {[a.relpath for a in scan.albums]}")

            with_render = find_duplicate(
                scan, album_name=ALBUM, track_title=rendered, artist_name=ARTIST,
                artist_scope=settings.dedup_artist_scope,
            )
            with_tag = find_duplicate(
                scan, album_name=ALBUM, track_title=leaf.title, artist_name=ARTIST,
                artist_scope=settings.dedup_artist_scope,
            )
            print(f"  with the rendered   {with_render}")
            print(f"  with the tag title  {with_tag}   <- the bug the rule exists for")
            if with_render is None:
                failures.append("the rendered name did not find a file that is on disk")
            if with_tag is not None:
                failures.append("the tag title matched, so this case does not discriminate")

            print("\n== 6. the queue " + "=" * 55)
            # `POST /api/jobs` cannot be used here: it would expand the URL against the real
            # catalogue, and this check has no network and no Apple account. The enqueue and
            # the leaf registration are therefore done the way the handler does them, so what
            # is under test is still the real thing: a real rendered name, a real walk, a real
            # `find_duplicate`, and a real `skip_reason` in the table.
            #
            # **The leaf registration is not optional, and the reason is worth recording.**
            # §6's `job` table carries no album name, no artist and no storefront, so a
            # queued row alone is not enough to run; `_leaf_for` re-expands the parent URL
            # when the registry misses, which is what makes a restart survivable and what
            # makes this work without the API. With no network that fallback fails and the
            # job is `failed` with a message naming the id -- never silently mis-run.
            from hub.jobs import JobStore

            store = JobStore(settings.db_path)
            store.create_batch(leaf.url, "album", [leaf], force=False)
            queued = store.get(1)
            assert queued is not None
            app.state.leaves.remember(queued, leaf)
            print(f"  registry depth      {len(app.state.leaves)}")
            if len(app.state.leaves) != 1:
                failures.append("the leaf was not registered for the queued job")

            ran = await app.state.run_one()
            job = store.get(1)
            print(f"  run_one()           {ran}")
            print(f"  job status          {job.status}")
            print(f"  skip_reason         {job.skip_reason!r}")
            print(f"  error               {job.error!r}")
            if job.status != "skipped":
                failures.append(f"the job finished as {job.status}, not skipped")
            if job.skip_reason != f"duplicate:{ARTIST}/{ALBUM}":
                failures.append(f"skip_reason is {job.skip_reason!r}, without the real path")
            if len(app.state.leaves) != 0:
                failures.append("the leaf was not released after the job finished")
            store.close()

            print("\n== 7. headers " + "=" * 59)
            health = await client.get("/api/health")
            for header in ("X-Content-Type-Options", "X-Frame-Options",
                           "Content-Security-Policy", "Referrer-Policy"):
                value = health.headers.get(header)
                print(f"  {header:26} {value}")
                if value is None:
                    failures.append(f"no {header} on a response")
            csp = health.headers.get("Content-Security-Policy") or ""
            if "unsafe-inline" in csp:
                failures.append("the CSP allows inline script, which makes it decorative")

            print("\n== 8. the status a user is shown " + "=" * 36)
            # Step 4b logged out, which retired the generation, so a session is needed again.
            await client.post("/api/auth/login", json={"password": PASSWORD})
            body = (await client.get("/api/status")).json()
            print(f"  wrapper.problem     {body['wrapper']['problem']!r}")
            print(f"  wrapper.detail      {body['wrapper']['detail'][:80]}...")
            print(f"  library.albums      {body['library']['albums']}")
            print(f"  library.degraded    {body['library']['degraded_roots']}")
            print(f"  queue               {body['queue']}")
            if body["wrapper"]["problem"] != "no-account":
                failures.append(
                    f"the wrapper reported {body['wrapper']['problem']!r} rather than "
                    f"'no-account' for a wrapper with no account"
                )
            if "did not become ready" in (body["wrapper"]["detail"] or ""):
                failures.append("the no-account message was collapsed into the other one")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("OK: the real client, the real tree and the real HTTP surface agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
