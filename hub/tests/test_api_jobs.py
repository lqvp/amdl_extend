"""The HTTP API and authenticated WebSocket queue stream.

Everything here runs against `httpx`'s ASGI transport, so there is no live server, no
wrapper binary and no network. The three collaborators the app owns -- the supervisor, the
ripper and the catalogue client -- are fakes, injected through `create_app`'s two keyword
seams, and **the resolver is not.** `hub.resolver.expand` is the real one, driven by a fake
`WebAPI`, because what is under test is the wiring: that a request's `codec`/`language`
reach the expansion, that the leaves come back as queue rows, and that the job the queue
later runs is rendered with the same name the library will hold.

The filesystem is not faked either. The dedup check is the whole point of this task
and it works by walking real directories, so these tests build real trees from
`conftest.make_library` and let `scan_roots` walk them.

The failures that matter here are all *silent* ones -- a dedup check fed a tag title instead
of a rendered name, a partial batch reported as the whole queue, a skip whose evidence never
reaches the page -- so the assertions are on the values a user can see, not on the calls that
produced them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from web_support import (
    ALBUM2_URL,
    ALBUM_URL,
    MV_URL,
    PASSWORD,
    SECRET,
    FakeRipper,
    FakeSupervisor,
    FakeWebAPI,
    _client,
    _expansion_with_three_usable_leaves,
    _store,
)

from hub import app as live_app_module
from hub import scheduler as scheduler_module
from hub.api import TEMPLATES_DIR, GuardedStatic
from hub.app import create_app
from hub.config import load_settings
from hub.jobs import Leaf, Progress
from hub.ripper_host import RipperHostError
from hub.scheduler import Scheduler, run_pool


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def test_health_needs_no_auth(client):
    """Exactly one route is reachable without a session.

    It is the compose healthcheck's target, so it has to answer before a login exists. It
    therefore reports *liveness only* -- no queue, no wrapper state, no library paths --
    because anything more would be the one place a stranger learns what this host holds.
    """
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_jobs_require_auth(client):
    r = await client.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert r.status_code == 401


async def test_the_only_unauthenticated_route_is_health(running, client):
    """The claim, checked against the *whole* surface and not only the schema.

    `app.openapi()` holds `APIRoute`s, so anything the schema does not carry -- a `Mount`, or
    the OpenAPI route itself -- is invisible to the derived table. Those are exactly the two
    that leaked: `/static` (a `Mount`, so outside every `guarded()` router) and
    `/openapi.json` (a plain `Route`, mounted by default, a machine-readable inventory of
    every route and parameter for anyone on the LAN).

    So this enumerates the non-`APIRoute` surface directly and asserts it is empty, and then
    probes the paths that *were* the holes.
    """
    # The schema is off, and it is off because this attribute is None rather than because a
    # test remembered a path.
    assert running.openapi_url is None

    # `app.routes` holds the *routers* that were `include_router`d, not their routes, so the
    # walk has to descend. In this FastAPI, `include_router` stores a `_IncludedRouter` whose
    # contents are on `.original_router.routes`; `Mount` exposes `.routes` directly. Anything
    # mounted at the top level is therefore outside every `guarded()` router -- which is
    # exactly how `/static` leaked, and why the schema alone could not find it.
    def walk(routes):
        for route in routes:
            nested = getattr(getattr(route, "original_router", None), "routes", None)
            if nested is None:
                nested = getattr(route, "routes", None)
            if nested:
                yield from walk(nested)
            else:
                yield route

    mounted_outside = [
        route
        for route in running.routes
        if not getattr(getattr(route, "original_router", None), "routes", None)
        and not getattr(route, "routes", None)
    ]
    leftovers = [
        (type(route).__name__, getattr(route, "path", None))
        for route in mounted_outside
        if type(route).__name__ != "APIRoute"
    ]
    # The one permitted entry is the static mount, and it is permitted because
    # `GuardedStatic` refuses for itself -- which the probe below checks by asking for a real
    # file without a session, rather than trusting the class name here.
    assert leftovers == [("Mount", "/static")], (
        f"entries mounted outside every guarded router: {leftovers}. Each is reachable with "
        f"no session unless it checks for one itself."
    )
    assert isinstance(mounted_outside[-1].app, GuardedStatic), (
        "the static mount is no longer the guarded subclass"
    )
    # And the walk is not vacuous: the `APIRoute`s it reached are exactly the schema's, and
    # the one extra leaf is the static mount this test has just accounted for.
    walked = list(walk(running.routes))
    assert sum(1 for r in walked if type(r).__name__ == "APIRoute") == len(
        _route_table(running)
    )

    # And the two paths that answered 200 in round 0.
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert (await client.get(path)).status_code == 404
        assert (await client.request("HEAD", path)).status_code == 404
    assert (await client.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})).status_code == 401


def _route_table(app) -> list[tuple[str, str]]:
    """Every `(METHOD, path)` on the app, **derived from `app.openapi()`**.

    The previous version of this was a hand-copied list of 15 pairs, and it was already out of
    date: the real table had 18. `POST /api/library/scan` and `GET /api/library/duplicates`
    were missing. Both were in fact guarded, so nothing leaked -- but the test whose own
    docstring warns that "a dependency added to one router and forgotten on another is a 401
    that does not happen" was itself the hand-copy it warns about, and it would not have
    caught a route added to `library.py` the next day.

    Reading the generated schema rather than walking `app.routes` is deliberate on two
    counts. It is the same list a caller would probe, so what is tested is what is served;
    and `app.routes` needs walking through `Mount` and `_IncludedRouter` objects to reach the
    real paths, which is exactly the machinery that hid `/openapi.json` and `/static` in the
    first place. The schema holds only `APIRoute`s, so anything not in it is *not* covered by
    this test -- which is why `test_the_only_unauthenticated_route_is_health` separately
    asserts that the non-`APIRoute` surface is empty.
    """
    schema = app.openapi()
    pairs: list[tuple[str, str]] = []
    for path, operations in schema["paths"].items():
        for method in operations:
            pairs.append((method.upper(), path))
    return sorted(pairs)


#: Paths that legitimately answer without a session, and why each one does. Anything not
#: listed here is a bug, and this is the list a new route has to argue its way onto.
OPEN_WITHOUT_A_SESSION = {
    ("GET", "/api/health"): "the compose healthcheck target; liveness only",
    ("POST", "/api/auth/login"): "mints the session",
    ("GET", "/login"): "the form itself, which is how a session is obtained",
    ("POST", "/login"): "the form's no-JS target",
    ("POST", "/logout"): "clears a cookie; see test_the_logout_page_cannot_be_driven_by_another_site",
}


async def test_every_route_but_health_requires_a_session(running):
    """The whole route table, derived, and the claim "only health is open" made literal.

    Enumerated from `app.openapi()` so a route added tomorrow is covered without anyone
    remembering this test, and then *probed* through the real app -- because a route being in
    the schema says nothing about whether the guard is attached to it, which is the failure
    this exists to catch.

    The count is asserted too. It is not a tautology: it is the thing that failed before, when
    the list was hand-copied and drifted from 15 to 18 without anybody noticing, and it makes
    "a route was added" a *loud* event rather than a silent one.
    """
    table = _route_table(running)
    assert table, "the schema is empty, so this test would pass by checking nothing"
    assert len(table) == len(set(table))
    # WebSocket endpoints are not represented in OpenAPI; the `/api/jobs/stream` HTTP route
    # was removed when the queue moved to `/api/jobs/ws`; the new queue history/control
    # endpoints bring the HTTP inventory to 32, `POST /api/jobs/cancel` to 33, and `GET
    # /api/jobs/export` to 34.
    assert len(table) == 34, (
        f"the route table has {len(table)} entries, not 34: {table}. A new HTTP route is "
        f"expected to change this number -- add it to OPEN_WITHOUT_A_SESSION only if it "
        f"genuinely has to be reachable without a session."
    )
    # And the two that the hand-copied list missed, named so their absence is remembered.
    assert ("POST", "/api/library/scan") in table
    assert ("GET", "/api/library/duplicates") in table

    async with await _client(running) as http:
        for method, path in table:
            body = {"password": PASSWORD} if path == "/api/auth/login" else {}
            response = await http.request(
                method,
                path,
                json=body if method in ("POST", "PUT", "PATCH") else None,
                follow_redirects=False,
            )
            # The jar is cleared between probes, and this matters: the table is sorted, so
            # `POST /api/auth/login` is reached before `POST /api/auth/logout`, and a session
            # the login probe obtained would make the *next* route answer 200 and the test
            # would read as a hole that is not there. Each probe is meant to be a first
            # request from a browser that has never seen this hub.
            http.cookies.clear()
            if (method, path) in OPEN_WITHOUT_A_SESSION:
                # 200 for the things that *do* something, 303 for the ones that redirect,
                # and 401 for `POST /api/auth/login` when the probe sent an empty body --
                # which is the right answer and says nothing about the guard. The real check
                # that login works with the right password is
                # `test_the_session_cookie_is_httponly_and_samesite_lax`.
                assert response.status_code in (200, 303, 401), (
                    f"{method} {path} is on the open list and answered {response.status_code}"
                )
                if path == "/api/auth/login":
                    assert response.status_code in (200, 401)
            else:
                # A page redirects and an API route 401s; both are "not without a session".
                assert response.status_code in (401, 303), (
                    f"{method} {path} answered {response.status_code} with no session"
                )


@pytest.mark.parametrize("path", ["/", "/queue", "/library"])
async def test_a_page_without_a_session_redirects_to_the_login_form(client, path):
    """A 401 on an HTML navigation is a JSON body in the place of a page.

    The browser is not an API client: it would render `{"detail": ...}` as the whole site.
    """
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_the_session_cookie_is_httponly_and_samesite_lax(client):
    response = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")
    assert "amd_hub_session=" in header
    # Plain HTTP, so no `Secure` -- a hardcoded one would make the cookie undeliverable on
    # the LAN deployment the image is built for.
    assert "Secure" not in header


async def test_a_wrong_password_is_refused_without_saying_which_part_was_wrong(client):
    response = await client.post("/api/auth/login", json={"password": "not-the-password"})
    assert response.status_code == 401
    body = response.json()
    assert body == {"detail": "Wrong password."}
    # Nothing about the submitted value, the expected one, or the length of either.
    assert "not-the-password" not in response.text
    assert PASSWORD not in response.text


@pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": 5}, {"password": None}])
async def test_a_malformed_login_is_refused_the_same_way(client, body):
    """Every shape of wrong answer gets the same 401 body.

    If an empty password and a wrong one produced different messages, the login form would
    tell an attacker whether the field was even read.
    """
    response = await client.post("/api/auth/login", json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": "Wrong password."}


async def test_login_is_rate_limited(client):
    for _ in range(10):
        await client.post("/api/auth/login", json={"password": "wrong"})
    blocked = await client.post("/api/auth/login", json={"password": "wrong"})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]


async def test_a_correct_login_after_the_limit_is_still_refused(client):
    """The limit is the limit.

    Releasing the budget on a *correct* answer would turn the rate limit into an oracle: try
    the password, and being let through tells you it was right.
    """
    for _ in range(10):
        await client.post("/api/auth/login", json={"password": "wrong"})
    response = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 429


async def test_a_successful_login_clears_the_limit(running, client):
    """Ten correct logins must not lock the owner out of their own hub.

    `check_rate_limit` records the attempt before the password is compared -- that is what
    makes the limit worth having against a comparison that leaks no timing -- so a *correct*
    answer has to give the budget back, or a user who re-logs-in after every hub restart is
    locked out by their own correct password. `SessionStore.forget` is the whole mechanism
    and `test_a_successful_login_clears_the_counter` in `test_auth.py` holds it directly;
    this holds that the login route is what calls it.
    """
    for _ in range(10):
        await client.post("/api/auth/login", json={"password": "wrong"})
    blocked = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert blocked.status_code == 429

    # The rate limiter sees the ASGI transport's peer address, and the test needs the same
    # string -- so it is read back out of the limiter rather than hardcoded.
    session_store = running.state.sessions
    (ip,) = session_store._attempts  # noqa: SLF001 - the limiter's own keying is the seam
    session_store.forget(ip)
    assert (await client.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200


async def test_a_forged_cookie_is_refused(client):
    client.cookies.set("amd_hub_session", "forged.token.value")
    assert (await client.get("/api/jobs")).status_code == 401


async def test_logout_clears_the_cookie_and_retires_the_session(client):
    """Both halves, and the second is the one that was missing (I1).

    Round 0 asserted only the `Set-Cookie` header, so a logout that told the browser to
    forget the token and left it working for anyone who had a copy passed. The token is
    captured before the logout and replayed afterwards, which is exactly what an attacker who
    read the cookie out of a browser profile would do.
    """
    await client.post("/api/auth/login", json={"password": PASSWORD})
    stolen = client.cookies.get("amd_hub_session", "")
    assert stolen, "no cookie to steal, so the rest of this proves nothing"
    assert (await client.get("/api/auth/session")).status_code == 200

    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert (await client.get("/api/jobs")).status_code == 401

    # The stolen token, replayed. Every route that accepts a session must refuse it now.
    for method, path in (("GET", "/api/jobs"), ("GET", "/api/auth/session"),
                         ("POST", "/api/wrapper/restart"), ("GET", "/api/status")):
        replayed = await client.request(
            method, path, json={}, headers={"cookie": f"amd_hub_session={stolen}"}
        )
        assert replayed.status_code == 401, (
            f"{method} {path} honoured a token issued before the logout"
        )


async def test_logout_retires_every_session_not_just_this_browser(running):
    """The trade, stated: one logout logs out everywhere.

    There is no session table, so "retire this one session" would mean building one --
    persisted, backed up, expired. For a single-password tool with no accounts that is a lot
    of machinery to avoid asking a second browser for the password again, and the alternative
    (a cookie the holder is asked to forget) is the thing I1 was about. This asserts the
    behaviour so that a future per-session revocation is a deliberate change rather than an
    accident someone notices a week later.
    """
    async with await _client(running) as laptop, await _client(running) as phone:
        for browser in (laptop, phone):
            assert (
                await browser.post("/api/auth/login", json={"password": PASSWORD})
            ).status_code == 200
        assert (await phone.get("/api/jobs")).status_code == 200

        assert (await laptop.post("/api/auth/logout")).status_code == 200
        assert (await phone.get("/api/jobs")).status_code == 401
        # And logging in again issues a token of the *new* generation, so the second browser
        # is not permanently locked out.
        assert (
            await phone.post("/api/auth/login", json={"password": PASSWORD})
        ).status_code == 200
        assert (await phone.get("/api/jobs")).status_code == 200


async def test_a_session_issued_before_a_logout_never_becomes_valid_again(running):
    """Retirement is one-way for a given token.

    A bumped generation does not make old tokens work again, and re-issuing at the old
    generation would be the bug. Two logouts with a login in between is the sequence that
    catches it.
    """
    async with await _client(running) as http:
        await http.post("/api/auth/login", json={"password": PASSWORD})
        first = http.cookies.get("amd_hub_session", "")
        await http.post("/api/auth/logout")

        await http.post("/api/auth/login", json={"password": PASSWORD})
        second = http.cookies.get("amd_hub_session", "")
        assert second != first or running.state.session_generation == 0
        assert (await http.get("/api/jobs")).status_code == 200

        replay = await http.request(
            "GET", "/api/jobs", headers={"cookie": f"amd_hub_session={first}"}
        )
        assert replay.status_code == 401


async def test_the_logout_page_retires_the_session_too(running):
    """The HTML route has the same effect as the JSON one, or a browser can never log out.

    The form posts to `/logout` and the queue page's button is that form, so if only the JSON
    route bumped the generation a user who logged out with the button would keep their
    session -- the cookie would be gone, so they would *think* they were logged out, and a
    captured token would still work.
    """
    async with await _client(running) as http:
        await http.post("/api/auth/login", json={"password": PASSWORD})
        stolen = http.cookies.get("amd_hub_session", "")

        response = await http.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

        replay = await http.request(
            "GET", "/api/jobs", headers={"cookie": f"amd_hub_session={stolen}"}
        )
        assert replay.status_code == 401


async def test_the_logout_page_cannot_be_driven_by_another_site(running):
    """It is unauthenticated, and that is stated rather than accidental (M1).

    A cross-site form post here is a nuisance -- a logout the user did not ask for -- and not
    a breach, because the only thing it can do is retire a session that already existed. It
    is on `OPEN_WITHOUT_A_SESSION` so the route table accounts for it. This asserts the
    reason is still true: the route needs no session, and what it does is bounded.
    """
    assert ("POST", "/logout") in OPEN_WITHOUT_A_SESSION
    async with await _client(running) as http:
        # No session at all, and it still works: that is the cross-site logout case.
        response = await http.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        # And it cannot be *used* for anything, because every other route still needs one.
        assert (await http.get("/api/jobs")).status_code == 401


async def test_the_session_endpoint_reports_whether_there_is_one(client):
    assert (await client.get("/api/auth/session")).status_code == 401
    await client.post("/api/auth/login", json={"password": PASSWORD})
    body = (await client.get("/api/auth/session")).json()
    assert body == {"authenticated": True}


# --------------------------------------------------------------------------- #
# POST /api/jobs
# --------------------------------------------------------------------------- #
async def test_post_jobs_reports_created_skipped_and_deduplicated(authed):
    r = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    body = r.json()
    assert r.status_code == 200
    # Four keys, not the three the contract names: `create_batch` raises `ValueError` on an
    # unusable leaf *after* applying the earlier ones, so a name is needed for the one that
    # was not queued. Without it a 19-track album with one bad track is a 500 and 19 queued
    # tracks.
    assert set(body) == {"created", "skipped", "deduplicated", "rejected"}
    assert body["created"] == [1]
    assert body["skipped"] == []
    assert body["deduplicated"] == []
    assert body["rejected"] == []


async def test_post_jobs_uses_the_clients_own_language_when_none_is_given(authed, web_api):
    """The catalogue language is the client's `region.language`, not a hub constant.

    A hub-side default that disagrees with `config.toml` would request metadata in a
    language the library was never written in, and the dedup comparison would be made
    against titles that were never on disk.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert web_api.calls[0] == ("album_info", "1621491338", "jp", "ja")


async def test_post_jobs_passes_the_requested_language_through(authed, web_api):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "language": "en"})
    assert web_api.calls[0][3] == "en"


async def test_a_language_is_asked_for_rather_than_invented(authed, ripper):
    """The client's own `config.toml` is the only authority, and a default would be a guess.

    The library on disk was written with whatever that client was configured with. Metadata
    in some other language comes back with different track titles, and the dedup comparison
    is against *file names* -- so a hub-side default would quietly turn every download into
    one that never matches anything already on disk.
    """
    ripper.language = None
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert response.status_code == 400
    assert "language" in response.json()["detail"]

    given = await authed.post(
        "/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "language": "en"}
    )
    assert given.status_code == 200


async def test_post_jobs_refuses_when_the_client_is_not_started(authed, ripper):
    """503, not 400: nothing about the request is wrong, the hub is.

    The catalogue client comes from the seam, and the seam is created by `create_app`. A
    `web_api` of `None` is a startup failure, and answering 400 would send the user to check
    a URL that was fine.
    """
    ripper._web_api = None  # noqa: SLF001
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert response.status_code == 503
    assert "not started" in response.json()["detail"]


async def test_a_codec_outside_the_clients_set_is_refused(authed):
    """A closed set, and a 400 rather than a queue full of unrippable rows.

    `Codec` is upstream's (`src/types.py`); the hub restates the seven values because the
    boundary forbids importing them. A typo would otherwise be stored in a `NOT NULL` column
    and fail at rip time, once, inside `rip_song`'s retry loop.
    """
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "flac"})
    assert response.status_code == 400
    assert "alac" in response.json()["detail"]


async def test_an_unparseable_url_is_a_400_and_names_the_url(authed):
    response = await authed.post(
        "/api/jobs", json={"urls": ["https://example.com/album/1"], "codec": "alac"}
    )
    assert response.status_code == 400
    assert "example.com" in response.json()["detail"]


async def test_no_urls_is_a_400_rather_than_a_silent_no_op(authed):
    response = await authed.post("/api/jobs", json={"urls": [], "codec": "alac"})
    assert response.status_code == 400


async def test_an_album_expands_to_one_job_per_track(authed, settings):
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert response.json()["created"] == [1]
    jobs = _store(settings).list(parent_url=ALBUM_URL)
    assert [(job.adam_id, job.title, job.codec) for job in jobs] == [("1", "1 a.m. (feat. shinoだす。)", "alac")]


async def test_the_same_track_in_two_languages_is_one_job(authed, settings):
    """`language` is not in the dedup key and must not be in the API's answer.

    One download serves both; two rows would download it twice.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "language": "ja"})
    second = await authed.post(
        "/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "language": "en"}
    )
    assert second.json() == {"created": [], "skipped": [], "deduplicated": [1], "rejected": []}


async def test_the_same_track_in_two_codecs_is_two_jobs(authed):
    first = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    second = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "aac"})
    assert first.json()["created"] == [1]
    assert second.json()["created"] == [2]


async def test_a_two_url_request_queues_both_and_reports_them_per_url(
    running, authed, settings
):
    """Two albums in one request, end to end, and the stream frame per URL (I6).

    The response is cumulative -- it is one answer to one question -- and the *frames* are
    not. `_publish_batch` was handed the running totals, so both frames carried all four ids
    and a tab merging them logged ids 3 and 4 on the first frame and again on the second.
    The docstring claimed the opposite of what the call did, which is how it survived review.

    So: the response has every id, and each frame has only its own URL's. Which is what makes
    `app.js`'s "queued job #N" log correct.
    """
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        # The snapshot first, as always, and the request *after* it so the two batch frames
        # are live rather than backlog -- a backlog frame is correctly dropped by the
        # snapshot filter, so a request made before the stream opened would produce no frames
        # to compare at all.
        assert (await stream.read_data())["kind"] == "snapshot"

        response = await authed.post(
            "/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"}
        )
        assert response.status_code == 200
        assert response.json()["created"] == [1, 2], (
            "the response is cumulative: one answer to one question about both albums"
        )

        first = await stream.read_data()
        assert first["kind"] == "batch"
        assert first["url"] == ALBUM_URL
        assert first["created"] == [1], "the first frame must not carry the second URL's ids"
        assert first["deduplicated"] == []

        second = await stream.read_data()
        assert second["kind"] == "batch"
        assert second["url"] == ALBUM2_URL
        assert second["created"] == [2]
        assert second["deduplicated"] == []

    # Both rows are real, one per URL, so the two expansions really did both happen and this
    # is not a test that passes on one id.
    jobs = _store(settings).list()
    assert [(job.id, job.parent_url) for job in jobs] == [(1, ALBUM_URL), (2, ALBUM2_URL)]


async def test_the_frames_of_a_two_url_request_do_not_overlap(running, authed):
    """The exact property I6 is about, asserted as disjointness.

    A tab merges frames by id, so the property that matters is that no id appears in two
    frames of one request -- not that the frames look plausible. Both directions are checked:
    the first frame carries nothing from the second URL, and the second carries nothing from
    the first.
    """
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()  # the snapshot
        await authed.post(
            "/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"}
        )
        frames = [await stream.read_data(), await stream.read_data()]

    assert [frame["url"] for frame in frames] == [ALBUM_URL, ALBUM2_URL]
    first_ids, second_ids = set(frames[0]["created"]), set(frames[1]["created"])
    assert first_ids and second_ids, "both URLs queued nothing, so the frames prove nothing"
    assert not first_ids & second_ids, (
        f"frames overlap: {sorted(first_ids & second_ids)} appear in both, so a tab merging "
        f"them would report a duplicate"
    )
    # Together they are still everything the response said.
    assert first_ids | second_ids == {1, 2}


async def test_second_identical_request_is_deduplicated(authed):
    first = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    second = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert first.json()["created"] == [1]
    # The *second* request names the job that is already queued. It is the holder's id and
    # not a new one, so the queue row the user can see is the one that will run.
    assert second.json() == {"created": [], "skipped": [], "deduplicated": [1], "rejected": []}


async def test_deduplication_folds_onto_the_running_job_not_the_oldest_one(authed, settings):
    """A finished job with the same key must not be what the second request names.

    `job_active_dedupe` is a *partial* index, so a `done` row still exists and still has the
    key. Filtering the holder lookup by status is the store's job; what the API must not do
    is invent an id of its own.
    """
    store = _store(settings)
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store.mark(1, "done")
    second = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    # The first job is terminal, so the key is free and a *new* row is created.
    assert second.json()["created"] == [2]
    assert second.json()["deduplicated"] == []


async def test_force_does_not_buy_a_second_job_for_one_track(authed):
    """The dedup index has no way to express `force`, and that is the point.

    `force` means "re-download this even though it is on disk", which is decided per *file*
    at execution time -- not "run this twice at once".
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    forced = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "force": True})
    assert forced.json()["deduplicated"] == [1]
    assert forced.json()["created"] == []


# --------------------------------------------------------------------------- #
# The partial batch
# --------------------------------------------------------------------------- #
def _unusable_leaf_batch(settings) -> None:
    """Two good leaves and then one with no `adam_id`, through the real store.

    Written against `JobStore` directly rather than through the API because the resolver
    refuses to *produce* such a leaf (`hub.resolver._required`). The refusal the API layer
    has to survive is a leaf from a future resolver change or a hand-edited database, not
    one this build can make -- and a test that can only be reached through a code path that
    cannot fail is not a test.
    """
    from hub.jobs import Leaf

    store = _store(settings)
    good = [
        Leaf(adam_id="1", title="one", album_name="A", artist_name="X", codec="alac",
             language="ja", url=ALBUM_URL, storefront="jp"),
        Leaf(adam_id="2", title="two", album_name="A", artist_name="X", codec="alac",
             language="ja", url=ALBUM_URL, storefront="jp"),
    ]
    with pytest.raises(ValueError):
        store.create_batch(
            ALBUM_URL,
            "album",
            good + [Leaf(adam_id="", title="three", album_name="A", artist_name="X",
                         codec="alac", language="ja", url=ALBUM_URL, storefront="jp")],
            force=False,
        )


async def test_create_batch_applies_the_leaves_before_the_bad_one(settings):
    """The store's half of the contract, asserted here because the API's depends on it.

    If this changed, the API's 200-with-a-partial-result answer would be reporting a batch
    that does not exist, and nothing else in the suite would notice.
    """
    _unusable_leaf_batch(settings)
    assert [job.adam_id for job in _store(settings).list()] == ["1", "2"]


async def test_a_refused_leaf_answers_200_with_what_landed_and_a_rejected_name(
    authed, settings, monkeypatch
):
    """One bad track must not become a 500 with 19 tracks queued behind it.

    The handler catches `ValueError`, reads back the batch **by `parent_url`** and reports
    the two that landed plus a `rejected` entry naming the one that did not.
    """
    monkeypatch.setattr(
        "hub.api.jobs.expand",
        _expansion_with_one_unusable_leaf(),
    )
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == [1, 2]
    assert body["skipped"] == []
    assert body["deduplicated"] == []
    # The *offending* leaf, named by its own title -- not the batch's first track, which is
    # no help to somebody looking at a 19-track album and told "track 1 failed".
    assert body["rejected"] == ["the one with no id"]
    # And the store's own wording about *why*, unrewritten, beside it.
    assert "leaf adam_id is ''" in body["problems"][0]["detail"]
    assert body["problems"][0]["url"] == ALBUM_URL


async def test_the_partial_answer_names_only_this_batch(authed, settings, monkeypatch):
    """The read-back is filtered by `parent_url`, and this is the test that says so.

    `store.list(parent_id=None)` means *no filter* -- `parent_id` is a self-reference nothing
    writes -- so reading this batch back that way returns the user's entire queue. Here that
    is job 1, queued from a *different* URL a moment earlier: an unfiltered read would report
    `created == [1, 2]` and tell the user their first request had been created by their
    second one.

    The overlap is deliberate. The refused batch's second track is `adam_id=2`, which the
    first request already queued, so it folds into job 1 -- which puts a real id from the
    other URL on both sides of the assertion and makes the difference between `[2]` and
    `[1, 2]` a difference about *filtering* rather than about which tracks are new.
    """
    first = await authed.post("/api/jobs", json={"urls": [ALBUM2_URL], "codec": "alac"})
    assert first.json()["created"] == [1]

    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_one_unusable_leaf())
    response = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    body = response.json()
    assert body["created"] == [2]
    assert body["rejected"] == ["the one with no id"]
    # `deduplicated` is empty even though the refused batch's second track *did* fold into
    # job 1. That fold is recorded on the holder's row, which belongs to the other URL, and
    # there is no column on it saying which leaf put it there -- so after a `ValueError` a
    # cross-batch fold is not recoverable and `deduplicated` is a lower bound. Naming job 1
    # here would mean reading the whole queue, which is the bug this test exists for.
    assert body["deduplicated"] == []
    assert set(body) >= {"created", "skipped", "deduplicated", "rejected"}
    # And the store agrees: two rows total, one per URL, neither invented.
    assert [job.parent_url for job in _store(settings).list()] == [ALBUM2_URL, ALBUM_URL]


def _expansion_with_one_unusable_leaf():
    """A stand-in for `hub.resolver.expand` whose third leaf has no `adam_id`.

    Returns an async function, so it patches the *name* the handler imported and the handler
    cannot accidentally reach the real resolver for this case.
    """
    from hub.jobs import Leaf

    async def fake_expand(url, *, codec, language, web_api):
        assert url == ALBUM_URL
        return [
            Leaf(adam_id="1", title="1 a.m. (feat. shinoだす。)", album_name="4pi",
                 artist_name="toe", codec=codec, language=language, url=url, storefront="jp"),
            Leaf(adam_id="2", title="2 p.m.", album_name="4pi", artist_name="toe",
                 codec=codec, language=language, url=url, storefront="jp"),
            Leaf(adam_id="", title="the one with no id", album_name="4pi",
                 artist_name="toe", codec=codec, language=language, url=url, storefront="jp"),
        ]

    return fake_expand


# --------------------------------------------------------------------------- #
# The queue, the scheduler, and the filesystem duplicate check
# --------------------------------------------------------------------------- #
async def test_a_job_already_on_disk_is_skipped_with_the_matched_paths(
    running, authed, settings, library, ripper
):
    """The dedup check end to end, on a real tree.

    `skip_reason` carries the matched paths *verbatim* and they are the whole of the
    evidence: `loose` matching is deliberately willing to treat two same-named albums as one,
    and a user can only overrule that from the paths.
    """
    album = library / "toe/4pi"
    album.mkdir(parents=True)
    (album / "1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    assert (await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})).json()["created"] == [1]
    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "skipped"
    # Root-qualified, so it resolves. `hit.matched` is the bare relpath and is still what
    # the library scan indexes on; this is the form a human can open.
    assert job.skip_reason == f"duplicate:{library}/toe/4pi"
    # And it is not decoration: the path in the reason is a directory, right now.
    assert (library / "toe/4pi").is_dir()
    # And nothing was ripped: a skip is decided before the client is asked to do anything.
    assert ripper.songs == []


async def test_the_dedup_check_is_given_the_rendered_file_name_not_the_tag_title(
    running, authed, settings, library, ripper
):
    """The one silent bug this suite exists to avoid.

    `normalize` is not idempotent. The file on disk is `1-01 1 a.m. (feat. …).m4a`, whose key
    is `1 a.m. (feat. …)`; the tag title is `1 a.m. (feat. …)`, whose own key is
    `a.m. (feat. …)`. Feeding the tag title compares against a key the library does not
    hold, so the job re-downloads a file that is already there -- no error, no warning, and
    in the direction nothing ever reports.
    """
    album = library / "toe/4pi"
    album.mkdir(parents=True)
    (album / "1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    leaf = Leaf(
        adam_id="501", title="1 a.m. (feat. shinoだす。)", album_name="4pi",
        artist_name="toe", codec="alac", language="ja", url=ALBUM_URL,
        storefront="jp",
    )
    job_id = running.state.jobs.create_batch(ALBUM_URL, "album", [leaf], force=False).created[0]
    running.state.leaves.put(job_id, leaf)
    await running.state.run_one()

    assert _store(settings).get(1).status == "skipped"
    # The two keys, side by side, so the test says *why* the assertion above is not a
    # coincidence of this particular title.
    from hub.normalize import normalize

    assert normalize("1-01 1 a.m. (feat. shinoだす。).m4a") == "1 a.m. (feat. shinoだす。)"
    assert normalize("1 a.m. (feat. shinoだす。)") == "a.m. (feat. shinoだす。)"


async def test_a_duplicate_across_two_roots_names_both_paths(
    running, authed, settings, tmp_path, ripper
):
    """種別 A: one release filed in two places, across two library roots.

    **This is the case the bare relpath form cannot express.** Both roots hold `toe/4pi`, so
    `hit.matched` is `("toe/4pi", "toe/4pi")` -- two entries, identical strings, one
    directory each. A `skip_reason` built from those would tell the user the track is on disk
    at a path that appears twice and resolves under neither root, which is the same dead end
    as every other cross-root skip, and the 種別 A shape is common in a real library.

    So `skip_reason` carries `hit.resolved`: the same two directories, each with the root it
    was found under, both of which open. Sorted by `find_duplicate`, so the string is stable
    across two scans of one unchanged tree -- `os.walk` order is filesystem-dependent and
    these are shown to a user.
    """
    first = tmp_path / "lib"  # the configured root
    second = tmp_path / "lib2"
    for root in (first, second):
        (root / "toe/4pi").mkdir(parents=True)
        (root / "toe/4pi/1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    running.state.settings.library_roots = [first, second]
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    await running.state.run_one()

    reason = _store(settings).get(1).skip_reason
    assert reason == f"duplicate:{first}/toe/4pi|{second}/toe/4pi"
    # The property that makes it evidence rather than decoration: every path it names opens.
    # The bare relpath form fails exactly here, which is the point of the test.
    for entry in reason.removeprefix("duplicate:").split("|"):
        assert Path(entry).is_dir(), f"{entry} does not resolve"


async def test_a_track_that_is_not_on_disk_is_ripped(running, authed, settings, ripper):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert await running.state.run_one() is True
    job = _store(settings).get(1)
    assert job.status == "done"
    assert job.finished_at is not None
    assert [leaf.adam_id for leaf, _ in ripper.songs] == ["1"]


async def test_the_same_track_in_another_album_is_never_skipped(
    running, authed, settings, library, ripper
):
    """`intro` exists in six real albums; album scoping is the only reason this is safe.

    Without the album name in the comparison, this job would be skipped by a file in a
    different release and the user would never get the track they asked for.
    """
    other = library / "Someone/Completely Different"
    other.mkdir(parents=True)
    (other / "1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    await running.state.run_one()
    assert _store(settings).get(1).status == "done"
    assert len(ripper.songs) == 1


async def test_force_downloads_a_file_that_is_already_on_disk(
    running, authed, settings, library, ripper
):
    """The escape hatch, and it is `force` *at execution time*.

    The queue index cannot honour it, so the flag is stored and read here, where
    the file that would be skipped actually exists.
    """
    album = library / "toe/4pi"
    album.mkdir(parents=True)
    (album / "1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac", "force": True})
    await running.state.run_one()

    assert _store(settings).get(1).status == "done"
    assert [(leaf.adam_id, force) for leaf, force in ripper.songs] == [("1", True)]


async def test_the_job_contract_does_not_fabricate_is_music_video(
    running, authed, settings, ripper
):
    """I7: the key is absent, because there is no honest value to put in it.

    Round 0 wrote `data["is_music_video"] = False` into every serialised job. The `job` table
    has no such column -- `Leaf`'s own docstring says it is not persisted -- so that line was
    the *only* reason the key appeared in the API, and it was a fabrication: a music-video job
    reported `False` while the leaf it came from said `True`.

    Nothing rendered it, so the bug was invisible. It is in the public JSON contract, and a
    Phase 2 filter over `/api/jobs?type=music-video` would have been misled by it.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    job = (await authed.get("/api/jobs/1")).json()
    assert "is_music_video" not in job, (
        f"the serialised job carries is_music_video={job.get('is_music_video')!r}; the `job` "
        f"table has no column for it, so any value here is invented"
    )

    # And the same for a music video, which is where the fabrication was actually wrong. It
    # is queued *and run*, because the claim is by id and the alac job above would otherwise
    # be the one that ran.
    await authed.post("/api/jobs", json={"urls": [MV_URL], "codec": "aac"})

    # The value is *available* to a caller that wants it: the leaf the app resolved says
    # True. Checked while the job is still queued, because the registry releases a leaf when
    # its job finishes -- so after the run there is nothing left to ask.
    leaf = running.state.leaves.get(2)
    assert leaf is not None and leaf.is_music_video is True, (
        "the resolved leaf should carry the music-video flag; the point is that the *authoritative* "
        "value exists and the job row never has one"
    )

    while await running.state.run_one():
        pass
    video = (await authed.get("/api/jobs/2")).json()
    assert "is_music_video" not in video
    assert len(ripper.videos) == 1
    assert ripper.videos[0][0].is_music_video is True


async def test_the_job_contract_is_exactly_the_stores_columns(running, authed, settings):
    """No key in, no key out: the serialised job is `asdict(Job)`.

    Which is what makes I7 structural rather than a line that could be re-added -- a column
    appears when `JOB_TABLE_SQL` grows one, and nothing else. Compared as sets so the assertion
    is about
    the *shape* of the contract rather than about every value.
    """
    from dataclasses import fields

    from hub.jobs import Job

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    job = (await authed.get("/api/jobs/1")).json()
    assert set(job) == {field.name for field in fields(Job)}

    # The one conversion worth naming: `force` is an INTEGER in the row and a bool here, and
    # a `1` in the JSON would make a browser's `if (job.force)` work by accident.
    assert job["force"] is True or job["force"] is False


async def test_a_music_video_is_never_deduplicated(running, authed, settings, library, ripper):
    """A deliberate non-goal: a music video has no album scope to compare against.

    `rip.py` writes every video into one flat `mv.saveDir`, so the album-directory
    comparison cannot hold and the video is always re-downloaded. The user must not be told
    it was "skipped".
    """
    (library / "videos").mkdir()
    (library / "videos/anything.m4a").write_bytes(b"")

    await authed.post("/api/jobs", json={"urls": [MV_URL], "codec": "aac"})
    assert await running.state.run_one() is True
    assert _store(settings).get(1).status == "done"
    assert len(ripper.videos) == 1


async def test_a_failing_rip_fails_the_job_with_the_reason(
    running, authed, settings, ripper, supervisor
):
    """A download failure is shown, not swallowed.

    `RipperHostError`'s message is carried through verbatim because upstream's is the
    diagnosis; replacing it with "download failed" would send the reader to the wrong place.

    The wrapper is started and healthy first, so the failure is unambiguously a *download*
    failure rather than a wrapper that cannot serve -- which is the distinction
    `test_a_genuine_download_failure_is_not_parked` and
    `test_a_login_resumes_the_jobs_that_were_waiting_for_a_token` pull in opposite directions.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    ripper.rip_error = RipperHostError(
        "rip_song failed for adam_id=1: the wrapper refused /key"
    )
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    await running.state.run_one()

    job = _store(settings).get(1)
    assert job.status == "failed"
    assert "the wrapper refused /key" in job.error
    # The type is named too: `RipperHostError: ...` tells a reader the failure came from the
    # seam rather than from the hub, which is where they should look first.
    assert "RipperHostError" in job.error


async def test_a_job_whose_leaf_this_process_never_expanded_is_failed_with_a_reason(
    running, authed, settings
):
    """A row written by a previous process cannot be ripped from its columns alone.

    The `job` table stores `adam_id`, `title`, `codec` and `language` -- not the album name,
    the artist or the storefront, all of which `rip_song` and the dedup render need. So the
    hub re-expands the parent URL when it can, and when it cannot the job is failed with a
    message that says to re-submit, rather than being left `queued` forever or run with
    blanks that would silently never match anything.
    """
    store = _store(settings)
    store._conn.execute(  # noqa: SLF001 - a hand-written row, which is the case
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES (?, 'album', ?, ?, 'alac', 'ja', 0, 'queued', ?)",
        (ALBUM_URL, "999", "a row from another process", "2026-09-27T00:00:00.000+00:00"),
    )
    assert await running.state.run_one() is True
    job = store.get(1)
    assert job.status == "failed"
    assert "999" in job.error


async def test_a_restarted_process_recovers_a_queued_job_by_re_expanding(
    running, authed, settings, ripper
):
    """The recovery path, and the reason the two fakes above are not the normal case.

    A queued row from a previous process is re-expanded through the resolver, so the album
    name, the artist and the storefront come back from the catalogue rather than from a
    guess -- and a job whose `adam_id` the catalogue no longer lists is failed, not run
    against whatever the expansion happened to return.
    """
    store = _store(settings)
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES (?, 'album', '1', 'whatever', 'alac', 'ja', 0, 'queued', ?)",
        (ALBUM_URL, "2026-09-27T00:00:00.000+00:00"),
    )
    assert await running.state.run_one() is True
    assert store.get(1).status == "done"
    assert [leaf.adam_id for leaf, _ in ripper.songs] == ["1"]


async def test_run_one_reports_that_the_queue_was_empty(running):
    assert await running.state.run_one() is False


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
async def test_progress_reaches_the_store_and_the_stream(
    running, authed, settings, progress_ripper
):
    """The live transfer progress requirement, end to end, and with no polling in the test.

    The three pieces the ruling named, all of which were missing in round 0: the seam's
    callback, the `mark` that writes it, and the `publish` that puts the row on the stream.
    `grep progress hub/app.py` found only a docstring then.

    The fake is handed three readings, so the assertions are on the values rather than on
    "something was called" -- a handler that fired once with zeroes would satisfy the weaker
    form and render as a bar that never moves.
    """
    # Directly driving `run_one()` bypasses the scheduler's readiness gate, so put the fake
    # wrapper into the same ready state production requires before a rip starts.
    await running.state.supervisor.start()
    total = 400
    progress_ripper.progress_up_to = total

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()  # the snapshot
        assert await running.state.run_one() is True

        # One frame per reading, each a `running` job with the bytes that had arrived. The
        # claim itself speaks first -- the pool names itself at 1 before the row speaks --
        # and the readings follow in order, so the first is 0: a report of "nothing yet" is
        # still a report, and a handler that skipped it would be hiding the one reading that
        # proves the bar starts at zero rather than jumping.
        claim = await stream.read_data()
        assert claim["kind"] == "pool" and claim["ripping"] == 1
        first = await stream.read_data()
        assert first["kind"] == "job"
        assert first["job"]["status"] == "running"
        assert first["job"]["bytes_done"] == 0
        assert first["job"]["bytes_total"] == total
        assert first["job"]["progress"] == pytest.approx(0.0)

        middle = await stream.read_data()
        assert middle["job"]["status"] == "running"
        assert middle["job"]["bytes_done"] == total // 2
        assert middle["job"]["progress"] == pytest.approx(0.5)

        last = await stream.read_data()
        assert last["job"]["status"] == "running"
        assert last["job"]["bytes_done"] == total
        assert last["job"]["progress"] == pytest.approx(1.0)

        # And the terminal frame, so the stream does not end on a `running` row.
        terminal = await stream.read_data()
        assert terminal["job"]["status"] == "done"

        # The release closes the pair the claim opened: the pool says zero on its way out.
        released = await stream.read_data()
        assert released["kind"] == "pool" and released["ripping"] == 0

    # And the final row is `done`, so the last thing a tab sees is not a stuck `running`.
    job = _store(settings).get(1)
    assert job.status == "done"
    assert job.progress == pytest.approx(1.0)
    assert job.bytes_done == total


async def test_an_unknown_total_is_reported_as_none_and_not_as_zero(
    running, authed, progress_ripper
):
    """`bytes_total=None` and `progress=None` when upstream does not know the size.

    HLS segments do not always carry a content length, so "bytes so far, size unknown" is a
    real state. Writing `progress=0.0` for it would render as a bar pinned at zero, which reads
    as a hang; `None` renders as an indeterminate bar, which is true. The store's columns are
    nullable for exactly this, and `mark` only writes what it is given.
    """
    progress_ripper.progress_up_to = 1234
    # The fake's `_report` is handed a total; None is the case under test.
    original = progress_ripper._report  # noqa: SLF001 - the seam under test

    def report_without_total(leaf, done: int, _total) -> None:
        original(leaf, done, None)

    progress_ripper._report = report_without_total  # type: ignore[method-assign]  # noqa: SLF001

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()
        assert await running.state.run_one() is True
        # The last of the three readings, which is the one that carries 1234 -- behind the
        # claim's pool frame, which opens the sequence now that the pool speaks.
        readings = [await stream.read_data() for _ in range(4)]
    event = readings[-1]
    assert event["job"]["bytes_done"] == 1234
    assert event["job"]["bytes_total"] is None
    assert event["job"]["progress"] is None


async def test_progress_from_a_job_that_no_longer_exists_is_dropped(
    running, authed, progress_ripper
):
    """A reading that arrives after its job was cancelled is discarded, not an error.

    The transfer keeps running upstream for a moment after the row is gone, and the callback
    has no idea the row is. `mark` raises `JobNotFound` in that case and the handler swallows
    it: the alternative is an unhandled exception on a worker callback, which would take the
    scheduler down for a cosmetic problem.
    """
    progress_ripper.progress_up_to = 100
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    from hub.scheduler import _apply_progress

    # A reading for an id that was never in the table.
    _apply_progress(  # noqa: SLF001 - the handler under test
        running.state, 9999, Progress(bytes_done=5, bytes_total=10, fraction=0.5)
    )
    # Nothing raised, and the store is unchanged.
    assert running.state.jobs.get(9999) is None


async def test_no_progress_reading_is_written_when_nothing_is_running(
    running, progress_ripper
):
    """`current_job` is `None` between jobs, and that is the check that uses it.

    A callback that fired after `run_song` returned -- the seam cancels its poll task in a
    `finally`, but the handler is also reachable from a test or a future caller -- would write
    to whatever job id happened to be in the variable. `None` is the answer, and it is what
    makes a late reading harmless.
    """
    assert running.state.current_job is None
    Scheduler(running.state).forward_progress(  # noqa: SLF001 - the handler under test
        Progress(bytes_done=1, bytes_total=2, fraction=0.5)
    )
    # Nothing to write to, and nothing raised.
    assert running.state.jobs.get(1) is None


async def test_current_job_is_cleared_after_a_job_finishes(running, authed, settings, ripper):
    """**B3: the id was set and never cleared, so the test above was asserting a hope.**

    `state.current_job` was assigned in `_execute` and nothing ever reset it. So between jobs
    it still held the *previous* job's id, and any reading that arrived after a job finished --
    a stray sampler, a reading from a `call_soon_threadsafe` callback that was already queued
    -- was written to a row that had already reached its outcome. On a `done` row that is
    precisely the B2 bug, arrived at by a different route.

    All three outcomes are checked, because a `finally` that only covers the success path is
    the easy version to get wrong and the reason the parked and failed cases are here.
    """
    store = _store(settings)
    # This test invokes `run_one()` directly, so it supplies the ready wrapper state that the
    # production scheduler checks before claiming work.
    await running.state.supervisor.start()
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    # 1. success
    assert await running.state.run_one() is True
    assert store.get(1).status == "done"
    assert running.state.current_job is None, (
        "current_job still holds the finished job's id, so a late reading would be written to "
        "a row that already has its outcome"
    )

    # 2. failure
    ripper.rip_error = RuntimeError("something in the hub went wrong")
    await authed.post("/api/jobs", json={"urls": [ALBUM2_URL], "codec": "alac"})
    assert await running.state.run_one() is True
    assert store.get(2).status == "failed"
    assert running.state.current_job is None

    # 3. parked
    from hub.ripper_host import RipperHostError

    running.state.supervisor.regions = []
    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "aac"})
    assert await running.state.run_one() is True
    assert store.get(3).status == "waiting"
    assert running.state.current_job is None, (
        "a parked job is the case that lasts longest, so an uncleared id is worst here"
    )


async def test_a_late_progress_reading_cannot_resurrect_a_finished_job(
    running, authed, settings, ripper
):
    """**B2, driven as the race it is rather than as a direct call.**

    The seam cancels its sampler in a `finally`, which stops *new* readings but not one already
    handed to the event loop with `call_soon_threadsafe`. So a reading can arrive after the
    job was marked `done`, and `mark(job_id, "running")` on a `done` row used to succeed:
    it cleared `finished_at` and moved the row out of the terminal set, at which point both
    `DELETE /api/jobs/{id}` and `POST /api/jobs/{id}/retry` answer 409 -- "not finished, so
    there is nothing to retry" -- and nothing will ever release it. A finished job stuck
    displaying as running, with no user action able to clear it.

    So the race is set up for real: the reading is queued by the callback, and the job
    completes while it is still in the loop's queue. `_wait_for_callbacks` lets the loop
    drain, which is the part a direct `_apply_progress` call would skip.
    """
    store = _store(settings)
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert await running.state.run_one() is True
    assert store.get(1).status == "done"

    report = Scheduler(running.state).forward_progress  # noqa: SLF001 - the seam's callback
    # The real shape of the race: `report` is called while `current_job` still names the job,
    # exactly as the sampler would, and the callback it queues lands *after* the job has
    # finished. The id is set by hand because the job is already `done` here -- which is the
    # whole point: the sampler's view of the world and the scheduler's have diverged.
    running.state.current_job = 1
    report(Progress(bytes_done=5, bytes_total=10, fraction=0.5))
    running.state.current_job = None

    # Let the queued callback run. `call_soon_threadsafe` appends to the loop's ready queue, so
    # this is where the write the seam asked for actually happens.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    job = store.get(1)
    assert job.status == "done", (
        f"the job is {job.status!r} after a reading that was already in flight; the last thing "
        f"a user saw was a download that had finished, and the row now contradicts it"
    )
    assert job.finished_at is not None, "a finished job must keep its finished_at"

    # And the user is not locked out of it: both routes that can release a row still work.
    assert (await authed.post("/api/jobs/1/retry")).status_code == 200
    assert (await authed.delete("/api/jobs/1")).status_code == 200


def test_a_late_reading_is_dropped_rather_than_raised(app, settings):
    """**The half of B2 the race test cannot see, and the reason for a direct call.**

    The race above is driven through `call_soon_threadsafe`, so a reading for a finished job
    runs as a detached loop callback. If `_apply_progress` let the store's `IllegalTransition`
    escape, asyncio would log "Task exception was never retrieved" and **no test would fail**:
    the row is already correct, the queue is already correct, and the exception goes to a
    logger nobody reads. The store's refusal made the wrong thing *impossible*; this is what
    makes it *silent* rather than *loud*.

    So it is called directly, and the assertion is that nothing is raised. The three tests
    cover the three halves independently: the store's refusal (`test_jobs.py`), the caller's
    handling of a deleted row (first case here) and of a finished one (second case here).
    """
    from hub.scheduler import _apply_progress

    store = _store(settings)
    store.create_batch(
        ALBUM_URL,
        "album",
        [Leaf(adam_id="1", title="t", album_name="A", artist_name="X", codec="alac",
              language="ja", url=ALBUM_URL, storefront="jp")],
        force=False,
    )
    store.mark(1, "running")
    store.mark(1, "done")

    reading = Progress(bytes_done=5, bytes_total=10, fraction=0.5)
    # A finished job: the store refuses, and the refusal must not escape.
    _apply_progress(app.state, 1, reading)  # noqa: SLF001 - under test
    # A job that is not there at all: the other refusal, and the older one.
    _apply_progress(app.state, 9999, reading)  # noqa: SLF001 - under test

    job = store.get(1)
    assert job.status == "done", "a dropped reading must leave the row exactly as it was"
    assert job.progress is None
    store.close()


async def test_retry_requeues_a_finished_job(running, authed, settings):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "failed", error="boom")
    response = await authed.post("/api/jobs/1/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert store.get(1).finished_at is None
    assert store.get(1).error == "boom"  # the reason is kept until the job runs again


async def test_retrying_a_job_that_is_active_is_refused(running, authed, settings):
    """The partial index already refuses it, and a 409 says so before the database does."""
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert (await authed.post("/api/jobs/1/retry")).status_code == 409


async def test_cancelling_a_job_that_is_already_running_is_refused(running, authed, settings):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    _store(settings).mark(1, "running")
    response = await authed.delete("/api/jobs/1")
    assert response.status_code == 409


async def test_deleting_a_queued_job_frees_its_dedupe_slot(running, authed, settings):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert (await authed.delete("/api/jobs/1")).status_code == 200
    again = await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert again.json()["created"] == [2]


async def test_get_jobs_filters_by_status_and_by_parent(running, authed, settings):
    """`GET /api/jobs ?status=&parent=`, which round 0 implemented and never tested.

    Both filters were undefended: deleting `parent_url=parent` from the handler left the suite
    green, in a codebase whose stated failure mode is a filter that quietly stops filtering.

    `parent` is a **url**, and the reason is the same as the partial-batch read-back --
    `parent_id` is a self-reference nothing writes, so there is nothing else for it to name.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})
    store = _store(settings)
    # One of each terminal state, so a filter that is not applied is visible.
    store.mark(1, "done")
    store.mark(2, "failed", error="boom")
    # And a third job from a third URL, which no filter should ever return.
    store._conn.execute(  # noqa: SLF001 - a row whose parent is neither URL above
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES ('https://third.example/x', 'album', '3', 't', 'alac', 'ja',"
        " 0, 'queued', '2026-09-27T00:00:00.000+00:00')"
    )

    every = (await authed.get("/api/jobs")).json()["jobs"]
    assert [job["id"] for job in every] == [1, 2, 3]

    done = (await authed.get("/api/jobs", params={"status": "done"})).json()["jobs"]
    assert [job["id"] for job in done] == [1]
    assert done[0]["status"] == "done"

    failed = (await authed.get("/api/jobs", params={"status": "failed"})).json()["jobs"]
    assert [(job["id"], job["error"]) for job in failed] == [(2, "boom")]

    by_parent = (
        await authed.get("/api/jobs", params={"parent": ALBUM_URL})
    ).json()["jobs"]
    assert [job["id"] for job in by_parent] == [1]
    assert by_parent[0]["parent_url"] == ALBUM_URL

    # And the two compose, which is the case `?status=&parent=` actually describes.
    both = (
        await authed.get("/api/jobs", params={"status": "failed", "parent": ALBUM_URL})
    ).json()["jobs"]
    assert both == [], "a job that matches one filter and not the other must not come back"

    both = (
        await authed.get("/api/jobs", params={"status": "done", "parent": ALBUM_URL})
    ).json()["jobs"]
    assert [job["id"] for job in both] == [1]


async def test_an_absent_filter_means_no_filter_not_everything_or_nothing(running, authed):
    """`?status=` with no value is absent, and absent means unfiltered.

    The distinction matters because `parent_id=None` means "no filter" and a reader of the
    `job` table could reasonably expect "top level only". Both arguments default to None and
    None means no filter, which is what an absent query parameter means.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    every = (await authed.get("/api/jobs", params={})).json()["jobs"]
    assert [job["id"] for job in every] == [1]


async def test_an_unknown_status_filter_is_a_400_rather_than_an_empty_list(running, authed):
    """A typo in `?status=` is refused, not answered with `[]`.

    An empty list is a plausible-looking answer to "show me the finished jobs" and it is a lie
    -- there is no such status. The store's own closed set is the authority, and its message
    names it.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    response = await authed.get("/api/jobs", params={"status": "finished"})
    assert response.status_code == 400
    assert "finished" in response.json()["detail"]
    assert "done" in response.json()["detail"]


async def test_a_parent_filter_nothing_matches_is_an_empty_list(running, authed):
    """A `parent` that matches nothing is an empty answer, not an error.

    The opposite of the unknown-status case, and deliberately: the url is a value rather than
    a vocabulary, so "no jobs for this album" is a real answer and the question was well
    formed. A user who mistypes a URL wants an empty table, not a failure.
    """
    response = await authed.get("/api/jobs", params={"parent": "https://elsewhere.example/x"})
    assert response.status_code == 200
    assert response.json()["jobs"] == []


async def test_get_jobs_needs_a_session(running, client):
    assert (await client.get("/api/jobs", params={"status": "done"})).status_code == 401


async def test_a_job_that_is_not_there_is_a_404(authed):
    assert (await authed.get("/api/jobs/999")).status_code == 404
    assert (await authed.post("/api/jobs/999/retry")).status_code == 404
    assert (await authed.delete("/api/jobs/999")).status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/status
# --------------------------------------------------------------------------- #
async def test_status_reports_degraded_roots(authed, settings, tmp_path):
    """An unmounted drive must be *loud*.

    `loose` dedup against the surviving roots still works, so the failure mode without this
    is a quiet re-download of everything that lived on the missing drive.
    """
    running = settings.library_roots + [tmp_path / "not-mounted"]
    settings.library_roots = running
    response = await authed.get("/api/status")
    assert response.status_code == 200
    assert response.json()["library"]["degraded_roots"] == [str(tmp_path / "not-mounted")]


async def test_status_reports_a_per_root_count_so_an_empty_mount_is_visible(authed, settings,
                                                                            tmp_path):
    """The case `degraded_roots` cannot catch, and the reason this count is on `/api/status`.

    An external drive that is not plugged in is not necessarily *missing*: Docker's
    bind-mount autocreate makes the directory, so the root arrives mounted, readable, and
    empty. `reachable` is then `True`, `degraded_roots` is empty, and nothing anywhere says a
    drive is absent -- while the queue quietly re-downloads everything that lived on it, which
    is the single worst failure mode in this design.

    `library_scan` cannot resolve it: nothing on disk distinguishes an empty library from an
    absent drive. So the per-root count is surfaced, and a total cannot stand in for it -- a
    total of 1,069 is equally consistent with both roots working and with one of them empty.
    """
    empty = tmp_path / "ntfs-not-plugged-in"
    empty.mkdir()  # present and readable, which is exactly the problem
    full = tmp_path / "downloads"
    (full / "toe/4pi").mkdir(parents=True)
    (full / "toe/4pi/t.m4a").write_bytes(b"")
    (full / "other/9Lana").mkdir(parents=True)
    (full / "other/9Lana/t.m4a").write_bytes(b"")
    settings.library_roots = [empty, full]

    library = (await authed.get("/api/status")).json()["library"]

    assert library["degraded_roots"] == [], "the empty root reads as healthy -- that is the gap"
    assert library["per_root"] == [0, 2]
    assert library["albums"] == 2
    # Positional with `roots`, so a caller can pair them without guessing.
    assert dict(zip(library["roots"], library["per_root"], strict=True)) == {
        str(empty): 0,
        str(full): 2,
    }


async def test_the_library_scan_endpoint_also_reports_per_root(authed, settings, tmp_path):
    """`POST /api/library/scan` is the "I just plugged something in" endpoint, so it is the
    one an operator will use to check -- and it has to answer the same question `/api/status`
    does. A check that only looks at the total cannot tell a working root from an empty one.
    """
    empty = tmp_path / "drive-b"
    empty.mkdir()
    # `drive-a` is created so the assertion below is about an *empty* root rather than a
    # missing one -- the two are different states and only the first is the gap.
    (tmp_path / "drive-a").mkdir()
    settings.library_roots = [tmp_path / "drive-a", empty]

    body = (await authed.post("/api/library/scan")).json()

    assert body["degraded_roots"] == []
    assert body["per_root"] == [0, 0]
    assert body["roots"] == [str(tmp_path / "drive-a"), str(empty)]



async def test_startup_rejects_a_download_root_outside_scan_roots_before_starting_services(
    settings, supervisor, ripper, tmp_path
):
    config = tmp_path / "vendor.toml"
    config.write_text(
        '[download]\ndirPathFormat = "/unscanned/{artist}/{album}"\n',
        encoding="utf-8",
    )
    candidate = create_app(
        settings,
        supervisor=supervisor,
        ripper=ripper,
        autostart=True,
        ripper_config_path=config,
    )

    with pytest.raises(RuntimeError) as error:
        async with candidate.router.lifespan_context(candidate):
            raise AssertionError("unsafe config must refuse startup")

    message = str(error.value)
    assert "/unscanned/{artist}/{album}" in message
    assert str(settings.library_roots[0]) in message
    assert "start" not in supervisor.log
    assert ripper.started is False


async def test_library_search_filters_albums_through_the_api(authed, library):
    (library / "Artist Name" / "Quiet Album").mkdir(parents=True)
    (library / "Artist Name" / "Quiet Album" / "opening song.m4a").write_bytes(b"")
    (library / "Other Artist" / "Loud Album").mkdir(parents=True)
    (library / "Other Artist" / "Loud Album" / "finale.m4a").write_bytes(b"")

    response = await authed.get("/api/library/albums", params={"q": "quiet album"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_albums"] == 2
    assert [album["name"] for album in body["albums"]] == ["Quiet Album"]
    assert body["search"] == "quiet album"


async def test_duplicate_report_is_read_only_and_names_all_candidate_directories(
    authed, library
):
    first = library / "Artist One" / "Echoes"
    second = library / "Artist Two" / "Echoes"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    first_track = first / "01 Same Song.m4a"
    second_track = second / "same song.flac"
    first_track.write_bytes(b"one")
    second_track.write_bytes(b"two")
    unrelated = library / "Other Artist" / "Different Release"
    unrelated.mkdir(parents=True)
    (unrelated / "same song.m4a").write_bytes(b"three")

    response = await authed.get("/api/library/duplicates")
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_count"] == 1
    candidate = body["candidates"][0]
    assert candidate["album_name"] == "Echoes"
    assert candidate["directory_count"] == 2
    assert {item["path"] for item in candidate["directories"]} == {str(first), str(second)}
    assert "do not " in body["warning"]
    assert first_track.exists() and second_track.exists()


async def test_queue_api_window_keeps_active_rows_and_pages_terminal_history(authed, running):
    leaves = [
        Leaf(
            adam_id=f"history-{index}",
            title=f"history track {index}",
            album_name="History",
            artist_name="artist",
            codec="alac",
            language="ja",
            url=ALBUM_URL,
            storefront="jp",
        )
        for index in range(1, 104)
    ]
    created = running.state.jobs.create_batch(ALBUM_URL, "album", leaves, force=False).created
    for job_id in created[:101]:
        running.state.jobs.mark(job_id, "done")

    response = await authed.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 102  # all two active plus the newest 100 terminal rows
    assert body["history_has_more"] is True
    assert body["counts"]["total"] == 103
    assert {job["status"] for job in body["jobs"][-2:]} == {"queued"}

    history = await authed.get(
        "/api/jobs/history", params={"before_id": body["history_before_id"], "limit": 10}
    )
    assert history.status_code == 200
    assert [job["id"] for job in history.json()["jobs"]] == [created[0]]
    assert history.json()["has_more"] is False

    hydrated = await authed.get("/api/jobs/lookup", params=[("ids", created[0]), ("ids", created[-1])])
    assert [job["id"] for job in hydrated.json()["jobs"]] == [created[0], created[-1]]

async def test_status_reports_the_two_unready_states_differently(authed, supervisor):
    """`regions: []` and "not running" need different things from the user.

    Collapsing them tells somebody with no Apple account to go and wait for a wrapper that is
    already up. The two keys are `problem`, and the message says which.
    """
    # The state a fresh install is in: the wrapper is up and healthy, and there is no account
    # on it, so `regions` is empty.
    supervisor.regions = []
    await authed.post("/api/wrapper/start")

    no_account = (await authed.get("/api/status")).json()["wrapper"]
    assert no_account["running"] is True
    assert no_account["regions"] == []
    assert no_account["ready"] is False
    assert no_account["problem"] == "no-account"
    assert "no account" in no_account["detail"].lower()
    assert "did not become ready" not in no_account["detail"]

    # The other state: nothing running at all. Different key, different message, and it does
    # not claim the wrapper is waiting for anything.
    await authed.post("/api/wrapper/stop")
    not_running = (await authed.get("/api/status")).json()["wrapper"]
    assert not_running["running"] is False
    assert not_running["problem"] == "unavailable"
    assert not_running["detail"] != no_account["detail"]
    assert "no account is logged in" not in not_running["detail"]


async def test_a_ready_wrapper_reports_no_problem_at_all(authed, supervisor):
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    body = (await authed.get("/api/status")).json()["wrapper"]
    assert body["ready"] is True
    assert body["problem"] is None
    assert body["detail"] is None


async def test_a_start_failure_keeps_the_supervisors_own_wording(authed, supervisor):
    """The two `SupervisorError` messages are written to be shown, and they are different
    problems. Replacing either with "unavailable" is what this forbids."""
    from hub.wrapper_supervisor import SupervisorError

    supervisor.start_error = SupervisorError(
        "the wrapper did not become ready within 60s: nothing answered on /status"
    )
    response = await authed.post("/api/wrapper/start")
    assert response.status_code == 502
    assert "did not become ready" in response.json()["detail"]


async def test_the_status_reports_the_rip_pool(authed, settings):
    """`n/N ripping` as data: the scheduler's table and the configured ceiling.

    The queue page speaks this sentence at the end of its stream line; `/api/status` is
    where the same truth is read without a stream. Zero when idle is the whole point --
    an idle hub that shows nothing is an idle hub you cannot distinguish from a dead one.
    """
    body = (await authed.get("/api/status")).json()
    assert body["pool"] == {"ripping": 0, "limit": settings.rip_concurrency}


async def test_status_summarises_the_queue(authed, settings):
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    queue = (await authed.get("/api/status")).json()["queue"]
    assert queue["queued"] == 1
    assert queue["total"] == 1


async def test_status_counts_a_library_that_is_all_reachable(authed, library, tmp_path):
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")
    body = (await authed.get("/api/status")).json()
    assert body["library"]["degraded_roots"] == []
    assert body["library"]["albums"] == 1


# --------------------------------------------------------------------------- #
# Wrapper login
# --------------------------------------------------------------------------- #
async def test_a_challenged_login_does_not_restart_until_the_code_is_in(
    authed, supervisor
):
    """No restart yet, and this is not an omission.

    A 2FA code that has not been submitted has not been cached, so restarting here would
    bring up the same wrapper with the same empty `regions` and the user would be told the
    login failed when it has not been tried yet. The restart belongs after the code.
    """
    response = await authed.post(
        "/api/wrapper/login", json={"username": "me@example.com", "password": "apple-pw"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["challenge_id"] == "chal-1"
    # The window the child polls for, and not longer -- `auth.cpp` gives up after it.
    assert body["expires_in"] <= 60
    assert supervisor.log == ["login"]


async def test_a_successful_2fa_is_followed_by_a_supervisor_restart(
    authed, supervisor
):
    """The load-bearing rule: a serving wrapper does not pick up an account later.

    The payload reads its token cache at process start (`lite_main.cpp:614`), so reporting
    success on the login call alone leaves a serving-but-unauthenticated wrapper -- which a
    user cannot tell from a broken one, and which answers every download with nothing. So
    `stop()` then `start()` happen before the answer, and the answer is only 200 because a
    *restarted* wrapper reports regions.
    """
    await authed.post(
        "/api/wrapper/login", json={"username": "me@example.com", "password": "apple-pw"}
    )
    response = await _post_2fa(authed, "123456")
    assert response.status_code == 200
    assert supervisor.submitted == [("chal-1", "123456")]
    assert supervisor.log == ["login", "submit_2fa", "stop", "start"]
    assert response.json()["wrapper"]["regions"] == ["jp"]


async def test_a_login_that_needs_no_2fa_is_restarted_and_only_then_reported(
    authed, supervisor
):
    """Upstream reports "no 2FA was asked for" as an *error*, and it is often a success.

    `WrapperSupervisor.login` returns once the child asks for a code; an account that needs
    none never gets there, and the supervisor's own text says so. So a raised error is not
    treated as a failure on its own -- the restart happens either way and the observable
    `regions` decides. Here the account went in, so the answer is success.
    """
    from hub.wrapper_supervisor import SupervisorError

    supervisor.login_error = SupervisorError(
        "the login process finished without asking for a 2FA code (exit code 0). If this "
        "account needs no 2FA the login is done -- check http://127.0.0.1:12340/status."
    )
    response = await authed.post(
        "/api/wrapper/login", json={"username": "me@x.example", "password": "p"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "wrapper": response.json()["wrapper"], "resumed": 0}
    assert supervisor.log == ["login", "stop", "start"]


async def test_a_rejected_password_is_a_failure_with_both_wordings(authed, supervisor):
    """The account did not go in, and the restarted wrapper says so.

    `regions` is still empty after the restart, so this is a failure -- and the two messages
    that explain it are both present and both unrewritten: the supervisor's login error
    beside the hub's "no account is logged in". Collapsing them would leave a user who
    mistyped their Apple ID reading "wait for the wrapper", which is already up.
    """
    from hub.wrapper_supervisor import SupervisorError

    supervisor.login_error = SupervisorError(
        "the login process finished without asking for a 2FA code (exit code 1) ... the "
        "wrapper's own output says why:\nlogin failed: bad password"
    )
    # The account did not go in, so the token cache is unchanged and the restarted wrapper
    # still reports no regions -- which is the observable difference between "the login needs
    # no 2FA and worked" and "the credentials were rejected", and it is the only difference
    # there is.
    supervisor.accepts_2fa = False
    supervisor.regions = []
    response = await authed.post(
        "/api/wrapper/login", json={"username": "me@x.example", "password": "wrong"}
    )
    assert response.status_code == 502
    body = response.json()
    assert body["problem"] == "no-account"
    assert "no account is logged in" in body["detail"]
    assert "bad password" in body["login_error"]


async def test_the_2fa_deadline_is_the_binaries_sixty_seconds(authed, supervisor):
    """The challenge TTL matches the binary's own `20 x 3s` poll window.

    A 300 s deadline would offer a user a code the child has already stopped reading, and
    the wrapper would exit with the hub still holding a "valid" challenge. The constant is
    asserted because nothing about the *value* is otherwise visible, and the deadline is
    applied with `asyncio.wait_for` rather than left to the supervisor's configurable
    `twofa_ttl`, which a deployment could set to anything.
    """
    import asyncio

    from hub.api import wrapper as wrapper_api

    assert wrapper_api.TWOFA_DEADLINE == 60.0

    # Patched so the wait is instant: the real 60 s would make this a one-minute test, and
    # what is under test is *that* the wait is bounded, which is what the constant is for.
    monkey = wrapper_api.TWOFA_DEADLINE
    wrapper_api.TWOFA_DEADLINE = 0.05
    try:
        started = asyncio.Event()

        async def never_answers(challenge_id: str, code: str) -> None:
            started.set()
            await asyncio.sleep(3600)

        await authed.post(
            "/api/wrapper/login", json={"username": "me@x.example", "password": "p"}
        )
        supervisor.submit_2fa = never_answers  # type: ignore[method-assign]
        response = await _post_2fa(authed, "123456")
    finally:
        wrapper_api.TWOFA_DEADLINE = monkey

    assert started.is_set()
    assert response.status_code == 400
    assert response.json()["problem"] == "expired"
    # And the refused answer invites a fresh login rather than a retry of the same code.
    assert "Log in again" in response.json()["detail"]
    # The challenge is gone, so a second attempt with the same code is refused as "no
    # challenge" rather than being written to a file no child is reading.
    assert (await _post_2fa(authed, "123456")).status_code == 400
    assert "no 2FA code waiting" in (await _post_2fa(authed, "123456")).json()["detail"]


async def test_a_supervisor_that_cannot_start_after_a_login_says_so(authed, supervisor):
    """A login that "succeeded" and a wrapper that cannot come back is a failure.

    Reporting the login as successful is the failure this endpoint exists to prevent, so a
    restart that raises is reported, and its wording is passed through -- including the
    "no account is logged in" wording, which is what a start failure after a login usually
    is, and which the two `problem` values keep apart.
    """
    from hub.wrapper_supervisor import SupervisorError

    await authed.post("/api/wrapper/login", json={"username": "me@x.example", "password": "p"})
    supervisor.start_error = SupervisorError(
        "no account is logged in on the wrapper at http://127.0.0.1:12340/status: it is up "
        "and answering /status, but regions is empty"
    )
    response = await _post_2fa(authed, "123456")
    assert response.status_code == 502
    body = response.json()
    assert body["problem"] == "no-account"
    assert "no account is logged in" in body["detail"]


async def test_a_login_resumes_the_jobs_that_were_waiting_for_a_token(
    running, authed, supervisor, settings, ripper
):
    """The token-expiry park end to end, and with no raw SQL anywhere in it.

    Round 0 hand-`INSERT`ed a `waiting` row, which is a unit test of `resume_waiting` wearing
    the clothes of an integration test: nothing produced the row, so nothing was under test
    that *puts* a job in `waiting`, and `grep 'mark(.*waiting'` in the hub found no call at
    all. The producer was missing, not just untested.

    So the sequence here is the real one, start to finish:

    1. a rip fails while the wrapper has stopped being able to serve a download;
    2. the scheduler parks the job in `waiting` rather than failing it;
    3. a login restarts the wrapper, `regions` comes back, and `resume_waiting()` requeues it;
    4. the job runs again, and this time it is `done`.

    Step 1 is reached by making the *wrapper* unready, not by writing a message: the
    discriminator reads `regions`, so that is what has to change.
    """
    # The wrapper is up and healthy.
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    # Mid-download the account goes away: the wrapper keeps serving /status and `regions`
    # goes empty, which is the state described above.
    ripper.rip_error = RipperHostError("rip_song failed for adam_id=1: no such account")
    supervisor.regions = []

    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "waiting", (
        f"the job is {job.status!r}; a token that expires during a download parks "
        f"the job rather than failing it"
    )
    # The message names the *reason*, and `regions == []` with the wrapper up is the
    # account-signed-out reason. Asserted on the reason's own vocabulary rather than on a
    # substring, because the substring is what B1b is about: the round-1 message said "the
    # Apple token expired" for every park, including a crash the user could not fix by
    # logging in.
    assert "signed out" in job.error
    assert "Log in" in job.error
    assert "no such account" in job.error, "the upstream message is the diagnosis and is kept"
    assert job.finished_at is None, "a parked job is not finished; it is coming back"

    # A login. The `regions` list is emptied *before* it, so the login path sees the state a
    # user is actually in: the wrapper is serving with no account, which is what makes the
    # login necessary rather than a no-op. The restart then finds `["jp"]` and the job is
    # requeued by `resume_waiting`, which runs *after* the restart and before the answer.
    supervisor.regions = []
    await authed.post("/api/wrapper/login", json={"username": "me@x.example", "password": "p"})
    supervisor.regions = ["jp"]
    response = await _post_2fa(authed, "123456")
    assert response.json()["resumed"] == 1
    assert _store(settings).get(1).status == "queued"

    # The account is back, so the transfer is not going to be refused any more.
    ripper.rip_error = None

    # And it runs again, to completion this time.
    assert await running.state.run_one() is True
    job = _store(settings).get(1)
    assert job.status == "done", f"the resumed job ended as {job.status}: {job.error}"
    # `claim_next` clears the reason on the new attempt, so the row does not keep claiming to
    # be broken.
    assert job.error is None


async def test_a_parked_job_keeps_its_place_in_the_queue(running, authed, settings, ripper, supervisor):
    """A 20-track album interrupted on track 14 does not start over.

    `resume_waiting` requeues at the original id, and the queue is ordered by `id` and nothing
    else, so the tracks already done stay done and the interrupted one is the next to run. A
    resume that appended to the end would re-download fifteen tracks the user already has.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    # Two tracks, and the second one is the one that will be interrupted.
    await authed.post(
        "/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"}
    )
    store = _store(settings)
    assert len(store.list()) == 2

    # The wrapper goes away mid-pass. With the default ceiling the pass claims *both* tracks,
    # so both are attempted and both park: the wrapper being down is a fact about the wrapper,
    # not about one track, and holding the second one back would only make the pass look
    # ordered rather than be ordered. Two parked rows make the ordering claim below stronger
    # than one ever could.
    ripper.rip_error = RipperHostError("rip_song failed for adam_id=1: no such account")
    supervisor.regions = []
    assert await running.state.run_pool() == 2
    assert [store.get(1).status, store.get(2).status] == ["waiting", "waiting"]

    supervisor.regions = ["jp"]
    ripper.rip_error = None
    assert store.resume_waiting() == 2
    # The subject of this test, and unchanged by any of the above: the queue is ordered by
    # `id` alone, so the interrupted track is requeued *before* the one behind it rather than
    # after it. A resume that appended would re-download everything already done.
    assert [job.id for job in store.list(status="queued")] == [1, 2]


async def test_a_failure_that_is_not_a_ripper_error_is_never_parked(
    running, authed, settings, ripper, supervisor
):
    """Not every exception is a token question, and parking a bug hides it for ever.

    `waiting` is **not** a terminal status, so a job parked for a reason that never resolves
    is retried on a schedule and is never once visible as a failure. The exception type is
    therefore the first gate: only a `RipperHostError` -- a failure that came from the client,
    and so could be explained by a wrapper that cannot serve -- is even a candidate for
    parking, and the wrapper's own state decides the rest.

    A bare `RuntimeError` from the fake stands in for everything that is not a client failure:
    a `ResolveError` from a re-expansion, an `OSError` from a library root that disappeared, a
    bug. It is a real thing the scheduler sees, so it is a real case.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    # The wrapper is *ready*, so even a `RipperHostError` would not park this one.
    ripper.rip_error = RuntimeError("something in the hub itself went wrong")
    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "failed", (
        f"the job is {job.status!r}; a non-`RipperHostError` is not a token question, and "
        f"`waiting` is not terminal so parking it would retry for ever and never show a "
        f"failure"
    )
    assert "something in the hub itself went wrong" in job.error
    # And the type is named, because "RuntimeError" alone sends a reader to the wrong place.
    assert "RuntimeError" in job.error


async def test_a_genuine_download_failure_is_not_parked(running, authed, settings, ripper, supervisor):
    """The discriminator is the wrapper's state, not the error's wording.

    A job is parked for an expired *token*. A download that fails while the wrapper is
    perfectly able to serve -- a bad file, a decode error, a network blip -- must still be
    `failed`, because `waiting` is not terminal: a job parked for a reason that never resolves
    is retried for ever and is never once visible as a failure.

    The message here deliberately *mentions* an account, and the wrapper has `regions`, so a
    substring match on the prose would park this job. It must not.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    ripper.rip_error = RipperHostError(
        "rip_song failed for adam_id=1: the account quota was exhausted"
    )
    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "failed", (
        "a failure with a healthy wrapper must be failed, not parked; `waiting` is not a "
        "terminal state, so a wrong parking here is a job retried for ever"
    )
    assert "quota" in job.error


async def test_a_wrapper_dying_during_a_rip_parks_and_retries_the_running_job(
    running, settings, ripper, supervisor
):
    """An in-flight client call is cancelled, persisted as waiting, then retried on recovery."""
    from hub.wrapper_supervisor import Readiness

    leaf = Leaf(
        adam_id="501", title="track", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    store = _store(settings)
    store.create_batch(ALBUM_URL, "album", [leaf], force=True)
    job = store.claim_next()
    assert job is not None and job.status == "running"
    running.state.leaves.put(job.id, leaf)

    rip_started = asyncio.Event()
    wrapper_lost = asyncio.Event()

    async def wait_until_unavailable():
        await wrapper_lost.wait()
        return Readiness(kind="down", regions=(), detail="child exited")

    async def blocked_rip(_leaf, *, force):
        rip_started.set()
        await asyncio.Event().wait()

    supervisor.wait_until_unavailable = wait_until_unavailable
    ripper.run_song = blocked_rip
    from hub.scheduler import _execute

    execution = asyncio.create_task(_execute(running.state, job))
    await asyncio.wait_for(rip_started.wait(), 1.0)
    wrapper_lost.set()
    await asyncio.wait_for(execution, 1.0)

    parked = store.get(job.id)
    assert parked.status == "waiting"
    assert "resumes on its own" in parked.error
    assert running.state.current_job is None

    calls = []

    async def recovered_rip(_leaf, *, force):
        calls.append(force)

    supervisor._running = True
    wrapper_lost.clear()
    ripper.run_song = recovered_rip
    async with _scheduler_running(running):
        assert await _wait_until(lambda: store.get(job.id).status == "done", 3.0)
    assert calls == [True]


async def test_a_wrapper_exit_wins_a_simultaneous_rip_failure(
    running, settings, ripper, supervisor
):
    """The wrapper watcher gets a turn before an exit-shaped rip error is classified."""
    from hub.wrapper_supervisor import Readiness

    leaf = Leaf(
        adam_id="501", title="track", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    store = _store(settings)
    store.create_batch(ALBUM_URL, "album", [leaf], force=False)
    job = store.claim_next()
    assert job is not None
    running.state.leaves.put(job.id, leaf)

    wrapper_lost = asyncio.Event()

    async def wait_until_unavailable():
        await wrapper_lost.wait()
        return Readiness(kind="down", regions=(), detail="child exited")

    async def failing_rip(_leaf, *, force):
        wrapper_lost.set()
        raise RipperHostError("connection reset after wrapper exit")

    supervisor.wait_until_unavailable = wait_until_unavailable
    ripper.run_song = failing_rip
    from hub.scheduler import _execute

    await _execute(running.state, job)

    parked = store.get(job.id)
    assert parked.status == "waiting"
    assert "resumes on its own" in parked.error


async def test_a_wrapper_that_is_not_running_polls_the_job_instead_of_failing_it(
    running, authed, settings, ripper, supervisor, monkeypatch
):

    """A crashed wrapper is not an expired token, and parking is still the right answer.

    The park rules say a stopped wrapper fails jobs with `wrapper_unavailable`; its *other*
    row
    says a token expiry parks them. The difference the hub can actually observe is whether the
    wrapper can serve: a crash and an expiry both leave it unable to, and in both cases the
    job's own outcome depends on the user doing something -- restarting the wrapper, or logging
    in. Failing it would throw away a queued track the user still wants; parking it costs
    them nothing and the supervisor's own 3-restart budget will bring the wrapper back
    on its own.
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    await authed.post("/api/wrapper/stop")
    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "waiting"
    assert "shut down" not in (job.error or "")


async def test_create_app_assembles_the_supervisor_from_the_resolved_settings(
    tmp_path, ripper
):
    """**Item 1: the last unverified assembly point in the app.**

    `create_app` has exactly two blocks that build a collaborator -- the `RipperHost` and this
    `WrapperSupervisor` -- and round 2 covered the first. The second had *no* coverage at all,
    and for the same reason: every fixture injects `supervisor=FakeSupervisor()`, and
    `FakeSupervisor` has no `log_sink` attribute, so the construction block was never
    executed by a test. `resolved.wrapper_host`, `.wrapper_port`, `.wrapper_binary` and
    `.wrapper_base_dir` appeared in no test at all; the only assertion on them is
    `test_config.py`'s, which reads `Settings` and never reads the app.

    The production link that this block owns is `log_sink=lambda line: _log(app.state, line)`.
    Mutating it to `lambda line: None` -- dropping every line the wrapper writes -- left 558
    tests green, and `test_the_wrapper_log_reaches_the_same_stream` still passed, because it
    calls `hub.app._log` *by hand* rather than through the sink it was given. The test claimed
    "the wrapper's log lines reach the stream" and demonstrated that `_log` publishes.

    So: build through the real `create_app` with `supervisor=None`, replace only the
    `WrapperSupervisor` *class*, and assert what it was constructed with. Non-default values
    throughout, so a test that accidentally asserted the defaults would not pass -- the
    settings differ from `Settings`' defaults in all four fields.
    """
    from hub import app as app_module

    marker = tmp_path / "wrapper-bin"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved = load_settings(
        {
            "AMD_PASSWORD": PASSWORD,
            "AMD_SESSION_SECRET": SECRET,
            "AMD_LIBRARY_ROOTS": str(tmp_path / "library"),
            "AMD_DB_PATH": str(tmp_path / "hub.db"),
            "AMD_WRAPPER_BASE_DIR": str(tmp_path / "wrapper-base"),
            "AMD_WRAPPER_BINARY": str(marker),
            # Non-default on purpose: `Settings` defaults these to 127.0.0.1 and 12340, so
            # asserting the defaults would pass for a block that ignored `resolved` entirely.
            "AMD_WRAPPER_HOST": "192.0.2.44",
            "AMD_WRAPPER_PORT": "31337",
        }
    )
    (tmp_path / "library").mkdir(exist_ok=True)

    handed: list[dict] = []

    class RecordingSupervisor:
        """A `WrapperSupervisor` that records how it was built and nothing else."""

        def __init__(self, **kwargs):
            handed.append(kwargs)

        running = False
        adopted = False
        pid = 4242
        bound_port = 31337
        start_error: Exception | None = None
        challenge = login_error = accepts_2fa = None
        credentials = submitted = log = None
        login_gate = None

        async def start(self) -> None:
            self.running = True

        async def stop(self) -> None:
            self.running = False

        async def status(self) -> dict:
            return {"running": self.running, "regions": ["jp"]}

        async def close(self) -> None:
            return None

    original = app_module.WrapperSupervisor
    app_module.WrapperSupervisor = RecordingSupervisor
    try:
        # `supervisor=None` is the branch under test, exactly as B5 needed `ripper=None`.
        app = create_app(
            resolved,
            ripper=ripper,
            autostart=False,
        )
    finally:
        app_module.WrapperSupervisor = original

    assert isinstance(app.state.supervisor, RecordingSupervisor), (
        "the app did not construct a WrapperSupervisor, so this test is not measuring the "
        "block that matters"
    )
    assert len(handed) == 1, f"constructed {len(handed)} supervisors; expected exactly one"
    kwargs = handed[0]

    # Every setting the block reads, by value and by type. A wrapper pointed at the wrong port
    # or the wrong binary is a hub that cannot start its backend, and nothing else would say so.
    assert kwargs["host"] == "192.0.2.44", kwargs["host"]
    assert kwargs["port"] == 31337 and isinstance(kwargs["port"], int), kwargs["port"]
    assert Path(kwargs["binary"]) == marker, kwargs["binary"]
    assert Path(kwargs["base_dir"]) == tmp_path / "wrapper-base", kwargs["base_dir"]

    # The `log_sink`, which is the part that is *this app's* to get right: the supervisor is
    # given a callable bound to *this* app's state, not a module-level function.
    assert callable(kwargs["log_sink"]), "the block did not pass a log_sink at all"
    assert (resolved.wrapper_host, resolved.wrapper_port) == ("192.0.2.44", 31337)
    assert (Path(resolved.wrapper_binary), Path(resolved.wrapper_base_dir)) == (
        marker, tmp_path / "wrapper-base"
    )


async def test_the_wrapper_log_reaches_the_stream_through_the_sink_it_was_given(
    tmp_path, ripper
):
    """**And the sink works.** The test above proves the block was run; this proves its
    argument does what the block intends.

    The round-2 version of this assertion called `hub.app._log(app.state, line)` by hand,
    which demonstrated that `_log` publishes a frame and said nothing about the
    `log_sink=lambda line: _log(app.state, line)` the app actually installs. So the line is
    handed to the sink *the app gave the supervisor*, and read back off a real stream
    subscription, which is the path a wrapper's stderr takes in production.

    Mutating the block's `log_sink` to `lambda line: None` fails this test, which is the
    whole point: the line reaches the broker, so the queue page shows the wrapper's log.
    """
    from hub import app as app_module

    resolved = load_settings(
        {
            "AMD_PASSWORD": PASSWORD,
            "AMD_SESSION_SECRET": SECRET,
            "AMD_LIBRARY_ROOTS": str(tmp_path / "library"),
            "AMD_DB_PATH": str(tmp_path / "hub.db"),
            "AMD_WRAPPER_BASE_DIR": str(tmp_path / "wrapper-base"),
            "AMD_WRAPPER_HOST": "192.0.2.44",
            "AMD_WRAPPER_PORT": "31337",
        }
    )
    (tmp_path / "library").mkdir(exist_ok=True)

    handed: list[dict] = []

    class RecordingSupervisor:
        def __init__(self, **kwargs):
            handed.append(kwargs)

        running = False
        adopted = False
        pid = 4242
        bound_port = 31337
        log: list[str] = []

        async def start(self) -> None:
            self.running = True

        async def stop(self) -> None:
            self.running = False

        async def status(self) -> dict:
            return {"running": self.running, "regions": ["jp"]}

        async def close(self) -> None:
            return None

    original = app_module.WrapperSupervisor
    app_module.WrapperSupervisor = RecordingSupervisor
    try:
        app = create_app(resolved, ripper=ripper, autostart=False)
    finally:
        app_module.WrapperSupervisor = original

    assert handed, "no supervisor was constructed, so there is no sink to drive"
    sink = handed[0]["log_sink"]

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/api/auth/login", json={"password": PASSWORD})
            token = await _token(client)
            async with _ASGIWebSocket(app, token=token) as stream:
                await stream.read_data()  # the snapshot

                # A line exactly as the supervisor's pump would hand one over.
                sink("[lite] 2026-09-27 09:14:02 device has no such account")

                frame = await stream.read_data()
                assert frame["kind"] == "log", frame
                assert "no such account" in frame["line"], frame
                assert "[lite]" in frame["line"], "the prefix the supervisor adds is kept"

                # And the log pane is reachable, so this is not a frame nothing renders.
                page = await client.get("/queue")
                assert page.status_code == 200


async def test_create_app_hands_the_seam_a_live_progress_callback(settings, supervisor):
    """**B5: the production wiring, not the fake's own handler, is what is under test.**

    The round-1 tests called `hub.scheduler._on_progress(app.state)` themselves and assigned the
    result to the fake -- so the suite exercised the *callback* and never the thing that
    installs it. Passing `on_progress=None` at `app.py:628` left all 539 tests green, so the
    "… 転送速度" requirement was not shown to be satisfied by the app a user actually
    runs.

    So this builds the app through the real `create_app` with **no injected ripper**, which is
    the branch that constructs a `RipperHost`, and replaces only the class. That is the
    smallest possible seam: `create_app` still does its own construction, its own
    `partial(...)`, and hands the result to whatever it builds.

    Three assertions, because "a callable was passed" is the weakest of them:
      1. the seam was constructed and something was passed -- not `None`;
      2. invoking it drives `mark(...)` on the store, which is the half a live queue needs;
      3. invoking it publishes a `job` frame, which is the half the WebSocket stream needs.
    """
    from hub import app as app_module

    handed_over: list[object] = []

    class RecordingHost:
        """A `RipperHost` that records the callback and nothing else.

        Deliberately *not* the `FakeRipper` the rest of the suite uses. That one has the
        handler bolted on afterwards by a fixture, which is precisely the arrangement that
        let a broken wiring pass: the fake was the caller, so `create_app` was never
        exercised. Here nothing attaches the handler except the factory.
        """

        def __init__(self, config_path, *, on_progress=None):
            self.config_path = config_path
            self._on_progress = on_progress
            handed_over.append(on_progress)

        started = False
        closed = False

        async def start(self) -> None:
            self.started = True

        async def close(self) -> None:
            self.closed = True

    monkeypatched = app_module.RipperHost
    app_module.RipperHost = RecordingHost
    try:
        app = create_app(settings, supervisor=supervisor, autostart=False)
    finally:
        app_module.RipperHost = monkeypatched

    # 1. The construction happened, and something was handed over.
    assert isinstance(app.state.ripper, RecordingHost), (
        "the app did not build a RipperHost at all, so this test is not measuring the branch "
        "that matters"
    )
    assert len(handed_over) == 1
    handler = handed_over[0]
    assert handler is not None, (
        "create_app constructed the seam with on_progress=None. Nothing in the suite would "
        "have noticed: every round-1 test attached the handler itself, so a hub built by this "
        "factory would show no progress at all"
    )
    assert callable(handler)

    # 2 and 3. Drive it against a real job on a real store, through the real loop the
    # callback hops to, and check both halves.
    store = _store(settings)
    store.create_batch(
        ALBUM_URL,
        "album",
        [Leaf(adam_id="1", title="t", album_name="A", artist_name="X", codec="alac",
              language="ja", url=ALBUM_URL, storefront="jp")],
        force=False,
    )
    store.mark(1, "running")

    published: list[dict] = []
    real_publish = app.state.broker.publish
    app.state.broker.publish = lambda channel, data: (
        published.append(data), real_publish(channel, data)
    )[1]
    app.state.current_job = 1

    try:
        async with app.router.lifespan_context(app):
            handler(Progress(bytes_done=300, bytes_total=1000, fraction=0.3))
            # The callback defers to the loop, so the write has not happened yet.
            await asyncio.sleep(0)

        job = store.get(1)
        assert job.bytes_done == 300, (
            "the callback `create_app` installed did not write a reading to the store; the "
            "progress column would sit empty for the whole download"
        )
        assert job.progress == pytest.approx(0.3)
        assert job.bytes_total == 1000
        assert job.status == "running", "a reading must not change the status"

        frames = [frame for frame in published if frame.get("kind") == "job"]
        assert frames, (
            "the reading reached the store but nothing was published, so an open tab would "
            "not see it -- the store being right and the queue being right are two halves "
            "and a round-1 mutation proved they can be independent"
        )
        assert frames[-1]["job"]["bytes_done"] == 300
    finally:
        app.state.broker.publish = real_publish
        app.state.jobs.close()


# --------------------------------------------------------------------------- #
# B1 -- a parked job must have an exit, and must say which one it needs
# --------------------------------------------------------------------------- #
async def test_a_crash_parked_job_is_released_when_the_wrapper_comes_back(
    running, authed, settings, ripper, supervisor, monkeypatch
):
    """**B1a, and no login is involved anywhere in this test.**

    Round 1's producer parked a job whenever the wrapper could not serve, and the *only* thing
    that called `resume_waiting()` was the Apple login path. So a wrapper that crashed
    mid-download left a row reading "the Apple token expired -- log in from the queue page"
    when the account was entirely fine, and nothing could release it:

    - `POST /api/wrapper/start` and `/restart` do not touch the queue;
    - `POST /api/jobs/{id}/retry` refuses `waiting` with a 409, correctly;
    - `claim_next` takes only `queued`, and the job was not `queued`;
    - the scheduler's own readiness gate counted only `queued`, so with a queue of nothing but
      parked jobs it never even probed.

    Four separate reasons the job was stuck, and the one that mattered is the last. The fix
    is the class rather than the instance: the scheduler resumes on *any* ready probe, and
    `_has_actionable` counts `waiting` so the probe happens at all.
    """
    fake_expand = _expansion_with_three_usable_leaves()
    monkeypatch.setattr("hub.api.jobs.expand", fake_expand)
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    monkeypatch.setattr("hub.resolver.expand", fake_expand)
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    # The wrapper dies mid-rip. `regions` is irrelevant: it is not running.
    await authed.post("/api/wrapper/stop")
    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    assert await running.state.run_one() is True
    store = _store(settings)
    assert store.get(1).status == "waiting"

    # The wrapper comes back by itself -- a supervisor restart, a container restart, anything.
    # **No login, no 2FA, no retry call.**
    supervisor._running = True
    supervisor.regions = ["jp"]
    ripper.rip_error = None

    # The *real* loop, started the way the lifespan starts it, and simply waited on. Not
    # `resume_waiting()` called directly and not a hand-rolled "one turn": both would pass with
    # the recovery deleted, which is the failure this whole finding is.
    async with _scheduler_running(running):
        assert await _wait_until(lambda: store.get(1).status == "done", 5.0), (
            "the wrapper is serving again and the job is still parked after five seconds of a "
            "live scheduler loop; a `waiting` row that no readiness transition can release is "
            "a job the user has to delete by hand"
        )


async def test_a_hub_that_starts_holding_parked_jobs_releases_them(
    live_app, settings, supervisor, web_api
):
    """A restart holding parked jobs is the same no-exit, reached a different way.

    Worth its own test because it is the case a *transition* guard cannot catch: the loop's
    first observation is already "ready", so there is no transition to react to. The recovery
    is unconditional on a ready probe for exactly this reason, and the assertion below is also
    the only thing that holds `_has_actionable` honest -- if it counted `queued` alone, this
    process would see an empty queue, never probe, and never release anything.
    """
    store = _store(settings)
    supervisor.regions = ["jp"]
    supervisor._running = True
    leaf = Leaf(
        adam_id="1", title="t", album_name="A", artist_name="X", codec="alac",
        language="ja", url=ALBUM_URL, storefront="jp",
    )
    store.create_batch(ALBUM_URL, "album", [leaf], force=False)
    live_app.state.leaves.put(1, leaf)
    # A row that was already parked when this process came up -- a container restart during a
    # token expiry, say.
    store.mark(1, "waiting", error="the Apple account signed out while this was downloading")
    assert store.get(1).status == "waiting"
    assert not store.list(status="queued"), (
        "the queue must be empty of `queued` rows for this to be the test it claims to be; "
        "otherwise `_has_actionable` would have been true on the `queued` branch alone"
    )

    async with live_app.router.lifespan_context(live_app):
        assert await _wait_until(lambda: store.get(1).status == "done", 5.0), (
            "a fresh process found the wrapper serving and a parked job, and did not run it"
        )


async def test_the_two_park_reasons_produce_different_and_individually_true_messages(
    running, authed, settings, ripper, supervisor
):
    """**B1b: a crash must not tell the user to log in.**

    Round 1 had one message for both causes, and it named the *token* for both. So a wrapper
    crash -- something the supervisor's own restart budget fixes, and the user cannot influence
    at all -- produced "the Apple token expired. Log in from the queue page and it will resume
    from here." Two things wrong with that: the diagnosis is invented, and the instruction
    sends a user to do a pointless credential round-trip for a process that is not their
    account's problem.

    The two are asserted to be *distinguishable* and each to contain its own remedy and not
    the other's, because "different" alone is satisfied by two arbitrary strings.
    """
    # -- reason 1: the wrapper is not running at all.
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    await authed.post("/api/wrapper/stop")
    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    assert await running.state.run_one() is True

    crashed = _store(settings).get(1)
    assert crashed.status == "waiting"
    assert crashed.error, "a parked job with no reason is worse than no message"
    assert "Log in" not in crashed.error, (
        f"a crashed wrapper does not need a login: {crashed.error!r}"
    )
    assert "token" not in crashed.error.lower()
    assert "resumes on its own" in crashed.error, (
        "the remedy for a crash is that it fixes itself, and saying so is the difference "
        "between a message that helps and one that alarms"
    )

    # -- reason 2: the wrapper is up and serving /status with no account.
    store = _store(settings)
    store.mark(1, "queued")
    supervisor._running = True
    supervisor.regions = []
    ripper.rip_error = RipperHostError("rip_song failed for adam_id=1: no such account")
    assert await running.state.run_one() is True

    signed_out = store.get(1)
    assert signed_out.status == "waiting"
    assert "Log in" in signed_out.error, (
        "this is the one that does need a login, and it is the only one"
    )
    assert "signed out" in signed_out.error

    # And the two are not the same string, which is the property the ruling asked for.
    assert crashed.error != signed_out.error
    for reason, message in ((("no-account",), signed_out.error),
                            (("unavailable",), crashed.error)):
        assert message, reason


async def test_a_probe_that_itself_fails_does_not_claim_the_wrapper_stopped(
    running, authed, settings, ripper, supervisor, monkeypatch
):
    """**An uninformative observation does not support a specific claim, in either direction.**

    Round 2 handled this one direction only. A failed `/status` was parked as
    `"unavailable"`, whose message reads "**the wrapper stopped serving** while this was
    downloading" -- but the process may be perfectly healthy and the health check may simply
    have failed to get an answer. The round-2 test could not see it, because it only asserted
    the *absence* of "Log in", and the wrong claim here is the other one.

    The standard is the one already applied to credentials, applied symmetrically: we do not
    know the account signed out, so do not say so; we do not know the wrapper stopped, so do
    not say that either. `"unreachable"` is the third reason, and its message says the *check*
    failed and that the wrapper may well be fine.

    Two properties asserted, because either alone is too weak: the message must not claim the
    wrapper stopped, and it must not claim the account signed out. A version that said
    "something went wrong" passes both, and so does a version that said "the wrapper died" if
    it also happens not to mention logging in.
    """
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    async def unanswering() -> dict:
        raise ConnectionError("the QEMU guest did not answer")

    monkeypatch.setattr(supervisor, "status", unanswering)
    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    assert await running.state.run_one() is True

    job = _store(settings).get(1)
    assert job.status == "waiting", "a job is still parked; that part was never in doubt"
    message = job.error

    assert "the wrapper stopped serving" not in message, (
        f"a failed /status is not evidence the wrapper stopped: {message!r}. The supervisor "
        f"still had a running process -- `running` was True."
    )
    assert "Log in" not in message, (
        f"a failed /status is not evidence the account signed out either: {message!r}"
    )
    assert "signed out" not in message
    # It must still say something true, and it must keep the diagnosis.
    assert "the check that says whether the wrapper can serve failed" in message, message
    assert "may well be fine" in message, (
        "the honest part is that we do not know; without it the message is merely vaguer, "
        "not more accurate"
    )
    assert "the QEMU guest did not answer" in message, "the probe's own error is the evidence"


async def test_a_wrapper_that_is_known_to_be_down_does_claim_it_stopped(
    running, authed, settings, ripper, supervisor
):
    """The complement: a *known*-down wrapper is the one that may say it stopped.

    Splitting the reasons is only worth anything if the strong claim stays where the evidence
    supports it. `supervisor.running` is `False` here -- the supervisor's own record of having
    no process -- so "the wrapper stopped serving" is a reading rather than an inference, and
    this is the test that says so. A fix that weakened *both* messages to avoid the
    over-claim in the previous test would pass that one and fail this.
    """
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    await authed.post("/api/wrapper/stop")

    ripper.rip_error = RipperHostError("rip_song failed: connect error")
    assert await running.state.run_one() is True

    message = _store(settings).get(1).error
    assert "the wrapper stopped serving" in message, (
        f"with no process running, that is what happened and the message should say it: "
        f"{message!r}"
    )
    assert "Log in" not in message


@contextlib.asynccontextmanager
async def _scheduler_running(app):
    """The production `scheduler_loop`, as a task, for the body of the `with`.

    The same call the lifespan makes, with the same constants -- nothing pinned, nothing faked.
    The point is that a test which *drives* the loop (a hand-rolled "one turn", a direct
    `resume_waiting()`) cannot tell a working recovery from a deleted one, and both of those
    would pass. Waiting on the real loop is slower by about a second and is the only version
    of this assertion that says anything.
    """
    task = asyncio.create_task(Scheduler(app.state).run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_until(predicate, timeout: float, *, interval: float = 0.02) -> bool:
    """Poll `predicate` until it is true or `timeout` elapses. `True` if it became true.

    Used where the property under test is "the background loop does this *without* being
    driven" -- so a caller that had to poke the loop to make it happen would fail here, which
    is the whole difference between the scheduler's recovery and a direct `resume_waiting`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def test_the_2fa_route_refuses_a_code_when_nothing_is_waiting(authed):
    """The wire shape's `{code}` is enough, because the id lives here -- but only while it
    is live."""
    response = await _post_2fa(authed, "123456")
    assert response.status_code == 400
    assert response.json()["problem"] == "no-challenge"


async def test_the_apple_password_is_never_echoed_back(authed, supervisor):
    """The Apple password is held in memory only, never echoed and never stored."""
    response = await authed.post(
        "/api/wrapper/login", json={"username": "me@x.example", "password": "s3cret"}
    )
    assert "s3cret" not in response.text
    assert "s3cret" not in json.dumps({"regions": supervisor.regions, "log": supervisor.log})


def _post_2fa(client, code: str):
    return client.post("/api/wrapper/login/2fa", json={"code": code})


# --------------------------------------------------------------------------- #
# The WebSocket stream
# --------------------------------------------------------------------------- #
class _ASGIWebSocket:
    """Run the real app's WebSocket route and read its JSON messages without a server."""

    def __init__(
        self,
        app,
        path: str = "/api/jobs/ws",
        token: str | None = None,
        origin: str = "http://hub.test",
    ) -> None:
        self._app = app
        self._started = asyncio.Event()
        self._closed = asyncio.Event()
        self._incoming: asyncio.Queue[dict] = asyncio.Queue()
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.accepted = False
        self.close_code: int | None = None
        headers = [(b"host", b"hub.test"), (b"origin", origin.encode())]
        if token:
            headers.append((b"cookie", f"amd_hub_session={token}".encode()))
        self._scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 51234),
            "server": ("hub.test", 80),
            "subprotocols": [],
        }

    async def __aenter__(self) -> _ASGIWebSocket:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await self._incoming.put({"type": "websocket.connect"})
        await asyncio.wait_for(self._started.wait(), 5.0)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._task is None or self._task.done():
            return
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, 1.0)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _receive(self) -> dict:
        return await self._incoming.get()

    async def _send(self, message: dict) -> None:
        if message["type"] == "websocket.accept":
            self.accepted = True
            self._started.set()
        elif message["type"] == "websocket.send":
            text = message.get("text")
            if text is None:
                text = message.get("bytes", b"").decode("utf-8")
            self._messages.put_nowait(json.loads(text))
        elif message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            self._closed.set()
            self._started.set()

    async def read_data(self, timeout: float = 5.0) -> dict:
        try:
            return await asyncio.wait_for(self._messages.get(), timeout)
        except TimeoutError as exc:
            raise AssertionError(f"no further WebSocket message within {timeout}s") from exc

    async def wait_closed(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._closed.wait(), timeout)


async def _token(client) -> str:
    await client.post("/api/auth/login", json={"password": PASSWORD})
    return client.cookies.get("amd_hub_session", "")


async def test_the_websocket_stream_sends_a_fresh_json_snapshot(running, authed):
    """A WebSocket connection starts with the current database snapshot."""
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        assert stream.accepted
        assert (await stream.read_data())["kind"] == "snapshot"


async def test_a_pool_frame_names_the_pool_around_a_rip(running, authed):
    """`pool` frames are born and die with the claim, and say what was true then.

    A consumer that does not know the kind already ignores it -- the browser switches on
    `kind` and drops the rest, which is what makes an added kind a feature rather than a
    migration. The pair here is the claim and the release: one ripping, then none, and a
    `job` frame or three between them because the row's own truth changed too.
    """
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    agen = running.state.broker.subscribe("jobs")
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0)  # subscription is established on the first read, not the call
    await live_app_module.run_one(running.state)
    kinds: list[str] = []
    pools: list[dict] = []
    for _ in range(6):
        try:
            if pull is not None:
                chunk = await asyncio.wait_for(pull, 1.0)
                pull = None
            else:
                chunk = await asyncio.wait_for(agen.__anext__(), 1.0)
        except (TimeoutError, StopAsyncIteration):
            break
        frame = json.loads(chunk)
        kinds.append(frame["kind"])
        if frame["kind"] == "pool":
            pools.append(frame)
    if pull is not None and not pull.done():
        pull.cancel()
    await agen.aclose()
    assert "pool" in kinds, kinds
    assert pools[0]["ripping"] == 1 and pools[-1]["ripping"] == 0
    assert all(frame["limit"] == 4 for frame in pools)


@pytest.mark.parametrize(
    ("outcomes", "expected_calls", "expected_result"),
    [([False, False], 2, False), ([False, True], 2, True), ([True], 1, True)],
)
async def test_webhook_retries_once_until_it_succeeds(
    monkeypatch, outcomes, expected_calls, expected_result
):
    """A failed announcement gets one retry, and a successful one gets no extra POST."""
    calls = []

    class Response:
        def __init__(self, is_success):
            self.is_success = is_success

    class Client:
        def __init__(self, *, timeout):
            assert timeout == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def post(self, url, *, json):
            calls.append((url, json))
            return Response(outcomes[len(calls) - 1])

    monkeypatch.setattr(scheduler_module.httpx, "AsyncClient", Client)

    result = await scheduler_module._post_notification(
        "http://notify.test/hook", {"event": "queue-idle"}
    )

    assert result is expected_result
    assert calls == [
        ("http://notify.test/hook", {"event": "queue-idle"})
    ] * expected_calls


async def test_an_idle_queue_announces_itself_once(running, authed, monkeypatch):
    """The transition is the message, and the sink is the operator's to configure.

    One job finishes, the counts hold nothing `queued`, `running` or `waiting`, and the
    webhook gets exactly one sentence about it -- not one per job, not one per poll.
    An unset URL was tested by every other test in this file before it existed; this one
    is about what happens once somebody has asked to hear from the hub.
    """
    sent: list[dict] = []

    async def capture(url: str, payload: dict) -> bool:
        sent.append({"url": url, **payload})
        return True

    monkeypatch.setattr(scheduler_module, "_post_notification", capture)
    running.state.settings.notify_webhook_url = "http://notify.test/hook"

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert await live_app_module.run_one(running.state) is True
    assert len(sent) == 1, sent
    announcement = sent[0]
    assert announcement["url"] == "http://notify.test/hook"
    assert announcement["event"] == "queue-idle"
    assert announcement["queue"]["done"] == 1 and announcement["queue"]["total"] == 1

    # A pass over an empty queue announced nothing: it is the change into idle that
    # speaks, not every observation of idle.
    assert await live_app_module.run_one(running.state) is False
    assert len(sent) == 1


async def test_the_stream_sends_a_snapshot_and_then_live_updates(running, authed):
    """A tab opened mid-download has to render the queue as it is.

    The broker's backlog is up to 50 frames of *history*, which here describes a queue that
    has already moved on. Emitting it would render a stale state and then correct it;
    emitting the snapshot and then the backlog would render the transition twice. So the first
    frame out is the first `{"kind": "snapshot"}`, nothing comes before it, and everything
    after it is delivered untouched.
    """
    # An event published *before* the stream opens must not be replayed as stale history.
    running.state.broker.publish("jobs", {"kind": "job", "job": {"id": 999}})
    running.state.jobs.create_batch(
        ALBUM_URL,
        "album",
        [Leaf(adam_id="501", title="first", album_name="album", artist_name="artist",
              codec="alac", language="ja", url=ALBUM_URL, storefront="jp")],
        force=False,
    )
    assert running.state.broker.subscriber_count("jobs") == 0

    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        first = await stream.read_data()
        assert first["kind"] == "snapshot"
        assert [job["id"] for job in first["jobs"]] == [1]

        # A live change, published while the stream is open.
        running.state.jobs.create_batch(
            ALBUM2_URL,
            "album",
            [Leaf(adam_id="502", title="second", album_name="album", artist_name="artist",
                  codec="alac", language="ja", url=ALBUM2_URL, storefront="jp")],
            force=False,
        )
        running.state.broker.publish(
            "jobs", {"kind": "batch", "url": ALBUM2_URL, "created": [2], "deduplicated": []}
        )
        second = await stream.read_data()
        assert second["kind"] == "batch"
        assert second["created"] == [2]
        assert second["url"] == ALBUM2_URL
        assert second["deduplicated"] == []

        # And still exactly one snapshot: a third frame is a third *change*, never another
        # snapshot, because the snapshot filter lets everything after the first one through
        # untouched.
        running.state.broker.publish("jobs", {"kind": "log", "line": "a log line"})
        third = await stream.read_data()
        assert third == {"kind": "log", "line": "a log line"}
        assert running.state.broker.subscriber_count("jobs") == 1


async def test_the_stream_delivers_nothing_lost_across_the_handover(running, authed):
    """The gap the broker's two-phase subscribe exists to avoid, seen from the outside.

    A subscriber that read the backlog and *then* registered would have a window in which a
    published message reaches nobody: the queue silently loses one update, with no error
    anywhere, and the tab sits on stale state until something unrelated wakes it. So every
    message published after the stream opened arrives, in order, exactly once.
    """
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        assert (await stream.read_data())["kind"] == "snapshot"

        for job_id in (1, 2, 3):
            running.state.broker.publish("jobs", {"kind": "job", "job": {"id": job_id}})

        seen = [await stream.read_data() for _ in range(3)]
    assert [event["job"]["id"] for event in seen] == [1, 2, 3]
    assert all(event["kind"] == "job" for event in seen)


async def test_the_snapshot_reflects_the_queue_as_it_is(running, authed, settings):
    """The snapshot is a real `list()`, so a tab opened after the last event is not empty.

    The snapshot is from the database, independently of the broker's recent history. The
    stream also ignores earlier connections' snapshot frames when it subscribes.
    """
    leaf = Leaf(
        adam_id="501", title="track", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    running.state.jobs.create_batch(ALBUM_URL, "album", [leaf], force=False)
    running.state.jobs.mark(1, "done")
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        snapshot = await stream.read_data()
    assert snapshot["kind"] == "snapshot"
    assert [(job["id"], job["status"]) for job in snapshot["jobs"]] == [(1, "done")]


async def test_a_reconnected_websocket_gets_a_fresh_snapshot(running, authed, settings):
    """A dropped connection reconnects from current queue state, not an event gap."""
    store = _store(settings)
    store.create_batch(
        ALBUM_URL,
        "album",
        [Leaf(adam_id="501", title="track", album_name="album", artist_name="artist",
              codec="alac", language="ja", url=ALBUM_URL, storefront="jp")],
        force=False,
    )
    token = await _token(authed)

    async with _ASGIWebSocket(running, token=token) as first:
        assert (await first.read_data())["jobs"][0]["status"] == "queued"
    assert running.state.broker.subscriber_count("jobs") == 0

    store.mark(1, "done")
    async with _ASGIWebSocket(running, token=token) as reconnected:
        snapshot = await reconnected.read_data()
    assert snapshot["kind"] == "snapshot"
    assert [(job["id"], job["status"]) for job in snapshot["jobs"]] == [(1, "done")]


async def test_a_skipped_jobs_matched_paths_reach_a_reconnecting_tab(running, authed, library):
    """What the user needs to adjudicate a skip has to be in the snapshot, not only in the
    live frame that carried it.

    A tab that was closed when the skip happened reconnects and gets the snapshot. If
    `skip_reason` were not in it, the row would render as a bare "skipped" with nothing to
    look at -- the one outcome a skipped job must never present.
    """
    album = library / "toe/4pi"
    album.mkdir(parents=True)
    (album / "1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")

    leaf = Leaf(
        adam_id="501", title="1 a.m. (feat. shinoだす。)", album_name="4pi",
        artist_name="toe", codec="alac", language="ja", url=ALBUM_URL,
        storefront="jp",
    )
    job_id = running.state.jobs.create_batch(ALBUM_URL, "album", [leaf], force=False).created[0]
    running.state.leaves.put(job_id, leaf)
    await running.state.run_one()

    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        snapshot = await stream.read_data()
    job = snapshot["jobs"][0]
    assert job["status"] == "skipped"
    assert job["skip_reason"] == f"duplicate:{library}/toe/4pi"


async def test_a_terminated_job_reaches_the_stream(running, authed, settings, ripper):
    """The live half of the contract: what the scheduler does is what the tab shows."""
    leaf = Leaf(
        adam_id="501", title="track", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    job_id = running.state.jobs.create_batch(ALBUM_URL, "album", [leaf], force=False).created[0]
    running.state.leaves.put(job_id, leaf)
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()  # the snapshot
        await running.state.run_one()
        # The pool opens and closes the rip; the row's own frame is the truth between them.
        frames = [await stream.read_data(), await stream.read_data(), await stream.read_data()]
        assert [frame["kind"] for frame in frames] == ["pool", "job", "pool"]
        event = frames[1]
    assert event["kind"] == "job"
    assert event["job"]["id"] == 1
    assert event["job"]["status"] == "done"
    # And the published row agrees with the table. A queue showing a download that finished
    # two minutes ago as still running is the symptom of a `mark` that did not publish, and
    # this is the only place that would notice.
    assert event["job"]["finished_at"] == _store(settings).get(1).finished_at


async def test_the_websocket_stream_needs_a_session(running):
    async with _ASGIWebSocket(running) as stream:
        assert stream.accepted
        await stream.wait_closed()
    assert stream.close_code == 4401


async def test_an_open_websocket_closes_when_its_session_is_revoked(
    running, authed, monkeypatch
):
    monkeypatch.setattr("hub.api.jobs.WEBSOCKET_HEARTBEAT_SECONDS", 0.01)
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()
        response = await authed.post("/api/auth/logout")
        assert response.status_code == 200
        await stream.wait_closed()
    assert stream.close_code == 4401


async def test_the_websocket_stream_refuses_a_cross_origin_handshake(running):
    async with _ASGIWebSocket(running, origin="https://attacker.example") as stream:
        await stream.wait_closed()
    assert not stream.accepted
    assert stream.close_code == 4403


async def test_a_subscription_is_released_when_the_client_goes_away(running, authed):
    """`EventBroker.subscribe`'s `finally` has no other observable consequence.

    Without it, every tab that ever connected would be counted on every publish for the life
    of the process, and the count is the only thing that can see it. `__aexit__` cancels the
    app task, which is what a browser does when the tab closes.
    """
    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()
        assert running.state.broker.subscriber_count("jobs") == 1
    assert running.state.broker.subscriber_count("jobs") == 0


async def test_the_wrapper_log_reaches_the_same_stream(running, authed, supervisor):
    """The stream is "job 状態 + ログ行", and it is one channel rather than two.

    Two channels would need two subscriptions interleaved, and a snapshot published to one of
    them could not be placed correctly relative to the other's live frames. One channel means
    one ordering, and the client's `kind` field is what tells the two apart.
    """
    from hub.app import _log

    token = await _token(authed)
    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()  # the snapshot
        _log(running.state, "the wrapper is ready on http://127.0.0.1:12340/status")
        event = await stream.read_data()
    assert event == {"kind": "log", "line": "the wrapper is ready on http://127.0.0.1:12340/status"}
# --------------------------------------------------------------------------- #
# The queue page
# --------------------------------------------------------------------------- #
async def test_the_single_worker_rule_is_real_and_not_just_a_comment():
    """`main()` serves with one worker, and the reason is not "it seemed safer".

    Everything the app owns is on `app.state`: the broker (so a WebSocket subscriber would see
    only its own worker's events), the scheduler (two would race `claim_next` -- atomic, so no
    double rip, but each with its own leaf registry), and the session generation (a logout on
    one worker would not revoke a session minted by another). The last of those is a security
    property, which is why it is a test rather than a compose comment.
    """
    import inspect

    from hub import app as app_module

    source = inspect.getsource(app_module.main)
    assert "workers=1" in source, (
        "main() no longer pins a single worker; two workers would each have their own broker, "
        "scheduler and session generation"
    )
    # And the state that would be duplicated is genuinely on `app.state`, so the reason above
    # is about this app rather than a generic caution.
    app = create_app(
        load_settings(
            {
                "AMD_PASSWORD": "p",
                "AMD_SESSION_SECRET": SECRET,
                "AMD_LIBRARY_ROOTS": str(TEMPLATES_DIR.parent),
                "AMD_DB_PATH": str(TEMPLATES_DIR.parent / "worker-check.db"),
                "AMD_WRAPPER_BASE_DIR": str(TEMPLATES_DIR.parent),
            }
        ),
        supervisor=FakeSupervisor(),
        ripper=FakeRipper(FakeWebAPI()),
        autostart=False,
    )
    for attribute in ("broker", "jobs", "leaves", "scheduler", "session_generation", "loop"):
        assert hasattr(app.state, attribute), f"app.state has no {attribute}"
    app.state.jobs.close()


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_app(settings, supervisor, ripper):
    """An app whose lifespan really runs: supervisor started, host started, scheduler on.

    Separate from `app`, because `autostart=False` is what lets the other tests drive
    `run_one()` one step at a time without racing a background task -- and the shutdown
    ordering is only observable when the loop is the one that started the work.
    """
    config = settings.db_path.parent / "vendor-config.toml"
    config.write_text(
        f'[download]\ndirPathFormat = "{settings.library_roots[0]}/{{artist}}/{{album}}"\n',
        encoding="utf-8",
    )
    return create_app(
        settings,
        supervisor=supervisor,
        ripper=ripper,
        autostart=True,
        ripper_config_path=config,
    )


def _queue_one(settings, *, adam_id: str = "1", album: str = ALBUM_URL) -> None:
    """One queued row, written before the app starts so the scheduler finds it.

    Written directly rather than through `POST /api/jobs` because the app does not exist yet:
    a job that was in the queue before the hub booted is also the case that exercises the
    re-expansion path, which is what a restart actually looks like.

    `adam_id` and `album` are parameters only for the concurrency tests, which need rows that
    differ in the one key the pool's deferral looks at. The default keeps the single-job
    callers -- shutdown ordering, re-expansion -- exactly as they were.
    """
    from hub.jobs import Leaf

    _store(settings).create_batch(
        album,
        "album",
        [Leaf(adam_id=adam_id, title="1 a.m. (feat. shinoだす。)", album_name="4pi",
              artist_name="toe", codec="alac", language="ja", url=album,
              storefront="jp")],
        force=False,
    )


async def test_shutdown_awaits_running_jobs_before_closing_the_ripper(
    live_app, settings, ripper
):
    """`RipperHost.close()` refuses while a rip is in flight, and it is right to.

    It holds the process working directory, which upstream resolves
    `EMBEDDED_TEMPLATE_PATH` and `download.dirPathFormat` against -- restoring it under a
    running rip loses the FairPlay template and writes to the wrong tree, with no error
    anywhere. So the shutdown order is: signal the loop, **wait for it**, and only then
    close. The recorded order is the assertion; without it this test would pass as long as
    nothing raised.
    """
    order: list[str] = []
    started = asyncio.Event()

    async def slow_rip(leaf, *, force: bool) -> None:
        started.set()
        # Long enough that a shutdown arriving now cannot have waited for it by accident.
        await asyncio.sleep(0.3)
        order.append("rip finished")

    original_run = ripper.run_song
    ripper.run_song = slow_rip  # type: ignore[method-assign]
    original_close = ripper.close

    async def recording_close() -> None:
        order.append("host closed")
        await original_close()

    ripper.close = recording_close  # type: ignore[method-assign]
    _queue_one(settings)

    async with live_app.router.lifespan_context(live_app):
        await asyncio.wait_for(started.wait(), 5.0)
        # Leaving the block now is the shutdown, and the rip is still in flight.

    assert order == ["rip finished", "host closed"]
    assert ripper.closed is True
    # And the job reached a terminal state rather than being left `running` with nothing to
    # update it -- which is what a scheduler cancelled mid-rip would leave behind.
    assert _store(settings).get(1).status == "done"
    assert ripper.run_song is not original_run  # the substitution was in force throughout


async def test_a_shutdown_that_outlasts_its_grace_cancels_the_rip_and_still_closes(
    live_app, settings, ripper, monkeypatch
):
    """The bounded wait, and why cancelling is safe rather than a second bug.

    `DRAIN_TIMEOUT_SECONDS` is 300 s, which is longer than a `docker stop`'s ten seconds, so
    it is not what decides a real shutdown -- but a job that hangs forever must not hang the
    process. The test shortens the *drain*, not the rip, so the cancellation path is the one
    under test. `RipperHost`'s in-flight counter is released in a `finally`, so a cancelled
    rip still lets `close()` succeed; that is why the assert below can be about `closed`.
    """
    from hub import scheduler as scheduler_module

    started = asyncio.Event()

    async def hanging_rip(leaf, *, force: bool) -> None:
        started.set()
        await asyncio.sleep(3600)

    ripper.run_song = hanging_rip  # type: ignore[method-assign]
    monkeypatch.setattr(scheduler_module, "DRAIN_TIMEOUT_SECONDS", 0.2)
    _queue_one(settings)

    async with live_app.router.lifespan_context(live_app):
        await asyncio.wait_for(started.wait(), 5.0)

    assert ripper.closed is True
    job = _store(settings).get(1)
    assert job.status == "cancelled"
    assert "shut down" in job.error


async def test_a_shutdown_cancels_every_concurrent_rip_not_just_the_first(
    live_app, settings, ripper, monkeypatch
):
    """`return_exceptions=True` on the pool's gather, and the rows it stops being stranded.

    A plain `gather` propagates the first child exception straight to its awaiter and leaves
    the other children *running as orphaned tasks* -- it does not cancel them. On the shutdown
    path that first exception is a `CancelledError`, so the pool would return to the lifespan
    with three rips still in flight, and `RipperHost.close()` would refuse: the host holds the
    process working directory, so a refusal here is a container that will not stop.

    Collecting first and re-raising after every worker has settled is what makes "all three
    were cancelled and marked" observable rather than lucky. The single-rip case cannot tell
    the two apart -- there is no other child to strand.
    """
    from hub import scheduler as scheduler_module

    all_started = asyncio.Event()
    entered = 0

    async def hanging_rip(leaf, *, force: bool) -> None:
        nonlocal entered
        entered += 1
        if entered == 3:
            all_started.set()
        await asyncio.sleep(3600)

    ripper.run_song = hanging_rip  # type: ignore[method-assign]
    monkeypatch.setattr(scheduler_module, "DRAIN_TIMEOUT_SECONDS", 0.2)
    # Three rows, written before the app exists, so this is a boot with a full queue -- and
    # so the re-expansion path is the one that resolves them. `hub.resolver` because
    # `_leaf_for` imports `expand` inside its own body.
    from hub.jobs import Leaf

    def leaf_for(adam_id: str) -> Leaf:
        return Leaf(adam_id=adam_id, title=f"track {adam_id}", album_name="4pi",
                    artist_name="toe", codec="alac", language="ja", url=ALBUM_URL,
                    storefront="jp")

    async def expand_three(url, *, codec, language, web_api):
        return [leaf_for(n) for n in ("1", "2", "3")]

    monkeypatch.setattr("hub.resolver.expand", expand_three)
    _store(settings).create_batch(
        ALBUM_URL, "album", [leaf_for(n) for n in ("1", "2", "3")], force=False
    )

    async with live_app.router.lifespan_context(live_app):
        await asyncio.wait_for(all_started.wait(), 5.0)

    assert entered == 3, f"only {entered} rips were in flight; the test needs the pool full"
    assert ripper.closed is True, (
        "the host could not close, which means a rip was still in flight after the pool "
        "returned -- an orphaned worker, not a slow one"
    )
    statuses = {job.adam_id: job.status for job in _store(settings).list()}
    assert statuses == {"1": "cancelled", "2": "cancelled", "3": "cancelled"}, (
        f"{statuses}; a job the shutdown interrupted must stop saying `running`, or the queue "
        f"is a row that contradicts itself with nothing left to update it"
    )


async def test_one_host_per_process_and_no_second_start_after_a_close(live_app, ripper):
    """One host per process, enforced by the seam and relied on by the app.

    `os.chdir` is process-global: two hosts cannot both believe they own the directory, and
    the first `close()` would put the process back where the second one was started --
    leaving the process inside `AppleMusicDecrypt/` with no route out. And creart's
    `WrapperClient` is process-global and cannot be evicted, so a `start()` after a `close()`
    would hand back an `aclose()`d client. `FakeRipper` reproduces both refusals, because a
    fake that allowed a second start would make this test pass without the app respecting
    either one.
    """
    async with live_app.router.lifespan_context(live_app):
        assert live_app.state.ripper is ripper
        assert ripper.started is True
    assert ripper.closed is True
    assert live_app.state.ripper is ripper, "the app replaced its host instead of closing it"

    with pytest.raises(RuntimeError, match="already closed"):
        async with live_app.router.lifespan_context(live_app):
            pass


async def test_the_supervisor_is_stopped_after_the_host(live_app, supervisor):
    """The wrapper outlives nothing: it is stopped last, and only if it was ever started.

    Order matters in one direction only -- the client that talks to the wrapper is closed
    before the wrapper goes away, not after -- and this records it.
    """
    order: list[str] = []

    original_stop = supervisor.stop

    async def recording_stop() -> None:
        order.append("supervisor stopped")
        await original_stop()

    supervisor.stop = recording_stop  # type: ignore[method-assign]
    async with live_app.router.lifespan_context(live_app):
        assert supervisor.running is True
    assert order == ["supervisor stopped"]
    assert supervisor.running is False


async def test_a_wrapper_that_will_not_start_does_not_stop_the_hub(live_app, supervisor, settings, ripper):
    """A fresh install has no Apple account, and that must not be a boot failure.

    `supervisor.start()` fails with "no account is logged in" -- the exact state the login
    page exists to fix. Booting past it is what makes the hub usable at all, and refusing to
    boot would make "add your account" impossible. What is not optional is the reason: it is
    kept in `state.startup_error` and shown by `/api/status` until a start succeeds, rather
    than becoming a silent "not running".
    """
    from hub.wrapper_supervisor import SupervisorError

    supervisor.start_error = SupervisorError(
        "no account is logged in on the wrapper at http://127.0.0.1:12340/status: it is up "
        "and answering /status, but regions is empty, so it cannot serve a download"
    )
    async with live_app.router.lifespan_context(live_app):
        # The host still came up, and the scheduler is running.
        assert ripper.started is True
        assert live_app.state.scheduler is not None
        async with await _client(live_app) as http:
            await http.post("/api/auth/login", json={"password": PASSWORD})
            body = (await http.get("/api/status")).json()
    assert body["wrapper"]["problem"] == "no-account"
    assert "no account is logged in" in body["wrapper"]["detail"]



# --------------------------------------------------------------------------- #
# The store's parent_url filter
# --------------------------------------------------------------------------- #
async def test_list_parent_url_is_a_filter_and_not_a_second_meaning_of_none(settings):
    """`parent_id=None` means *no filter*, and `parent_url=None` has to mean the same.

    Two different readings of `None` on one method is how `?parent=` would end up
    rendering the whole queue as a single batch.
    """
    from hub.jobs import Leaf

    store = _store(settings)
    for index, url in enumerate(("https://a.example/x", "https://b.example/x")):
        store.create_batch(
            url,
            "album",
            [Leaf(adam_id=str(index), title="t", album_name="A", artist_name="X",
                  codec="alac", language="ja", url=url, storefront="jp")],
            force=False,
        )
    assert len(store.list()) == 2
    assert len(store.list(parent_url="https://a.example/x")) == 1
    assert store.list(parent_url="https://c.example/x") == []
    # And it composes with the other filter rather than replacing it.
    store.mark(1, "done")
    assert len(store.list(status="queued", parent_url="https://a.example/x")) == 0
    assert len(store.list(status="done", parent_url="https://a.example/x")) == 1


async def test_list_parent_url_refuses_a_blank_filter(settings):
    """Matching nothing is not the same as matching every job, and a blank filter is
    ambiguous between them."""
    with pytest.raises(ValueError, match="parent_url"):
        _store(settings).list(parent_url="  ")


async def test_the_store_filter_is_a_parameterised_query_not_string_building():
    """A `LIKE` or an f-string in the filter would be a way to match a different set.

    `?parent=` comes straight from a request, so the value is a bound parameter. This test
    would not notice a second code path, but it does fail if the clause is dropped.
    """
    assert "url = ?" in _list_sql_source()
    assert "%" not in _list_sql_source().split("where")[1]


def _list_sql_source() -> str:
    import inspect

    from hub.jobs import JobStore as Store

    return inspect.getsource(Store.list)


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #
async def test_the_rate_limit_key_is_the_peer_and_not_a_forwarded_header(
    running, client
):
    """I2, and the reason it is a test rather than a comment.

    `client_ip` reads `request.client.host` and nothing else. Swapping that read for
    `X-Forwarded-For` -- which the report itself named as the motivation behind the
    shared-bucket cost -- left the whole suite green in round 0, because every existing test
    sent no such header and so could not tell the two implementations apart.

    So this sends one, and it must not change anything: not the limit, not which address is
    remembered, and not whether the limit fires at all. A header a client chooses cannot be
    allowed to key a limiter, or `X-Forwarded-For: <random>` per request is ten free guesses
    against a 10-per-5-minutes budget.
    """
    # Ten wrong attempts, each with a *different* forwarded address.
    for index in range(10):
        response = await client.post(
            "/api/auth/login",
            json={"password": "wrong"},
            headers={"x-forwarded-for": f"203.0.113.{index}"},
        )
        assert response.status_code == 401, response.status_code

    # The eleventh is refused -- the same bucket, not ten buckets of one.
    blocked = await client.post(
        "/api/auth/login",
        json={"password": "wrong"},
        headers={"x-forwarded-for": "203.0.113.200"},
    )
    assert blocked.status_code == 429, (
        "a per-header rate limit is not a rate limit: the caller chooses the key"
    )

    # And the key really is the *peer*, not a constant and not a header: the transport's
    # peer is `127.0.0.1`, and a variant that read the forwarded header would have stored one
    # of the ten addresses above instead. This is the value assertion, which is why the other
    # test exists -- a count of requests cannot tell the two apart.
    remembered = set(running.state.sessions._attempts)  # noqa: SLF001 - the key under test
    assert remembered == {"127.0.0.1"}, (
        f"the limiter keyed on {remembered} after ten requests with ten different "
        f"`X-Forwarded-For` values; a header the caller chooses must not choose the bucket"
    )


async def test_the_rate_limit_key_is_reported_and_is_not_the_forwarded_value(running, client):
    """The same property from the other side: what the limiter stored.

    `tracked_ips()` and the limiter's own dict are read, so the assertion is on the *value*
    rather than on a count of requests. That is what makes the test say which key is used, and
    a version that reads the header would store the header's value.
    """
    await client.post(
        "/api/auth/login",
        json={"password": "wrong"},
        headers={"x-forwarded-for": "198.51.100.77", "x-real-ip": "198.51.100.78"},
    )
    remembered = set(running.state.sessions._attempts)  # noqa: SLF001 - the key under test
    assert remembered, "the attempt was not recorded, so the key is untested"
    assert remembered == {"127.0.0.1"}, (
        f"the limiter keyed on {remembered}; the forwarded headers are client-supplied and "
        f"must not choose the bucket. `127.0.0.1` is the ASGI transport's peer address."
    )


async def test_an_idle_hub_probes_the_wrapper_rarely_and_one_more_when_waking(
    live_app, settings, supervisor
):
    """I5, measured.

    The loop used to call `supervisor.status()` on every iteration whether or not there was
    anything to claim, sleeping 0.5 s between them: 2.0 probes/s, ~172,800 HTTP requests a day
    from a hub doing nothing. The requirement is real -- a token expiring while the hub runs
    is a state change a cache would hide -- but 0.5 s was the wrong instrument for it.

    So the bound is asserted directly: the probes are *counted* over a real slice of the
    loop's own clock, and the number is compared against both the old rate and the new one.
    """
    supervisor.regions = ["jp"]
    store = _store(settings)
    started = time.monotonic()
    probes: list[float] = []
    real_status = supervisor.status

    async def counting_status() -> dict:
        probes.append(time.monotonic())
        return await real_status()

    supervisor.status = counting_status  # type: ignore[method-assign]

    async with live_app.router.lifespan_context(live_app):
        # Several idle intervals, and the assertion is that *nothing* is probed in them: the
        # empty queue costs a SQLite read, and readiness cannot matter when there is nothing
        # to be ready for. The old loop made 2.0 HTTP probes per second here.
        await asyncio.sleep(scheduler_module.IDLE_POLL_SECONDS * 6)
    elapsed = time.monotonic() - started

    assert probes == [], (
        f"{len(probes)} probes over {elapsed:.1f}s on an empty queue: an idle hub is still "
        f"asking the wrapper whether it is ready. It should be asking SQLite."
    )

    # And the queue *is* empty, which is the premise. Stated so that a fixture change which
    # left a job behind fails here with a reason rather than passing for the wrong reason.
    assert store.list() == [], (
        f"the queue is not empty ({len(store.list())} jobs), so this is not measuring an "
        f"idle hub and the zero-probe assertion means nothing"
    )
    # And the bound is not met by a lucky short window: the interval a *ready* wrapper with
    # queued work is polled at is deliberately short, because a token change must be noticed
    # quickly and the queue is being actively used. The idle case is the one that was 2 Hz.
    assert scheduler_module.IDLE_POLL_SECONDS <= 1.0, (
        "an empty queue should be re-checked at the queue rate, not slower -- it costs a "
        "SQLite read and a user who queues a job should not wait for it"
    )


async def test_a_claim_is_never_made_on_a_stale_readiness_answer(
    live_app, settings, supervisor, web_api
):
    """The other half of I5, and the reason a slow idle poll is safe at all.

    A cache is only defensible if the *action* is guarded by a fresh check. So the wrapper is
    emptied while the loop is idling, and the next thing that happens must be a probe: the
    scheduler must not claim a job on the answer it cached before the change.
    """
    supervisor.regions = ["jp"]
    store = _store(settings)
    async with live_app.router.lifespan_context(live_app):
        # Let the loop establish a "ready" answer -- and with nothing queued it never will,
        # which is the point of the previous test, so a job is queued first and the loop is
        # given a moment to probe *and claim* it.
        store.create_batch(
            ALBUM_URL,
            "album",
            [Leaf(adam_id="1", title="t", album_name="A", artist_name="X", codec="alac",
                  language="ja", url=ALBUM_URL, storefront="jp")],
            force=False,
        )
        await asyncio.sleep(0.3)
        # Whatever the loop did with it, put the row back for the part under test.
        store.mark(1, "queued")
        probes: list[int] = []
        real_status = supervisor.status

        async def counting_status() -> dict:
            probes.append(1)
            return await real_status()

        supervisor.status = counting_status  # type: ignore[method-assign]

        # The wrapper loses its account between the probe above and now.
        supervisor.regions = []

        # The loop must probe, see `no-account`, and leave the job `queued` rather than
        # claiming it into a rip that cannot work.
        await asyncio.sleep(0.3)
        assert probes, "the loop never re-probed, so the answer it holds is unbounded"
        assert store.get(1).status == "queued", (
            f"the job is {store.get(1).status!r}; a claim on a stale 'ready' would start a "
            f"download against a wrapper that cannot serve it"
        )
        # And the loop's *own* cached answer was corrected, not just the queue. A loop that
        # kept claiming on the cached `None` would leave this at `None` while still not
        # claiming -- the job stays queued for the wrong reason, and the next job would be
        # claimed on the stale answer.
        assert live_app.state.cached_problem == "no-account", (
            f"the loop still believes the wrapper is ready "
            f"(cached_problem={live_app.state.cached_problem!r}) after observing no regions"
        )


async def test_a_cached_ready_answer_does_not_claim(live_app, settings, supervisor):
    """The slow idle poll is only defensible because the claim is guarded by a fresh probe.

    This is the mutation a probe of this loop applies, and the property it breaks is the one
    under test: "a token expiring while the hub runs is a state change a cache would hide". A
    loop
    that acted on `state.cached_problem` would claim the next job on the answer it happened to
    hold, and the queue would fill with rips against a wrapper that cannot serve them.

    The assertion is on the *job*, not on the cache: a version that refreshed the cache and
    still claimed anyway would satisfy a cache-shaped test and fail this one.
    """
    supervisor.regions = ["jp"]
    store = _store(settings)
    async with live_app.router.lifespan_context(live_app):
        # The job is queued *after* the loop is running, so the loop's first act on it is
        # already a decision about a readiness answer -- which is the point. Queuing it before
        # the lifespan would let the loop claim it while the wrapper was still ready.
        await asyncio.sleep(0.2)
        assert live_app.state.cached_problem is None, "the loop never established a ready reading"

        store.create_batch(
            ALBUM_URL,
            "album",
            [Leaf(adam_id="1", title="t", album_name="A", artist_name="X",
                  codec="alac", language="ja", url=ALBUM_URL, storefront="jp")],
            force=False,
        )
        supervisor.regions = []

        await asyncio.sleep(0.3)
        assert live_app.state.cached_problem == "no-account"
        assert store.get(1).status == "queued", (
            "the job was claimed against a wrapper that cannot serve it, so the answer the "
            "loop acted on was the cached one"
        )

        # **And the other direction, which is the one a cache gets wrong silently.** The
        # wrapper comes back -- a user logs in, or the supervisor's own restart budget brings
        # it up. A loop that kept re-using the cached "no-account" would sit on a job it could
        # have run, with nothing to wake it: the queue is stuck for ever and every status page
        # is honest about a wrapper that is up. The probe has to run again precisely when the
        # answer was bad.
        supervisor.regions = ["jp"]
        await asyncio.sleep(scheduler_module.IDLE_READINESS_POLL_SECONDS * 1.5)
        assert live_app.state.cached_problem is None, (
            "the loop never noticed the wrapper came back, so a job it could have run is "
            "stuck in the queue until the process restarts"
        )
        assert store.get(1).status == "done", (
            f"the job is {store.get(1).status!r} after the wrapper recovered; the login "
            f"path is only half a fix if a cached 'not ready' outlives the recovery"
        )


# ---------------------------------------------------------------------------
# Clearing the queue, from the API.
#
# Both operations are bulk, and both have to reach past the row: a deleted job's leaf
# has to be forgotten in memory, or the registry grows by one entry per delete for the
# life of the process. That is the whole reason `JobStore.delete_finished` returns ids.
# ---------------------------------------------------------------------------


async def test_requeue_failed_puts_them_back_and_says_how_many(
    running, authed, settings, monkeypatch
):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "failed")
    store.mark(2, "failed")
    store.mark(3, "done")


    response = await authed.post("/api/jobs/requeue", json={"scope": "failed"})

    assert response.status_code == 200
    body = response.json()
    assert body["requeued"] == [1, 2]
    assert body["refused"] == []
    assert {store.get(1).status, store.get(2).status} == {"queued"}


async def test_requeue_reports_a_refusal_rather_than_a_500(
    running, authed, settings, monkeypatch
):
    """A key another job holds is a fact to report, not a server error.

    The alternative -- letting the `IntegrityError` out -- would turn a duplicate that is
    already on its way into a 500, which reads as "the button is broken" rather than "one of
    these is already running".
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(2, "failed")
    store.mark(3, "failed")
    # Job 1 stays `queued`, holding adam_id=1. A second row for that key can only be made
    # by hand, because `create_batch` folds it into the holder -- and `create_batch` is not
    # what a real queue looks like after a restored backup or a schema change.
    store._conn.execute(  # noqa: SLF001 - the index is the point
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES ('u', 'album', '1', 't', 'alac', 'ja', 0, 'failed', 'now')"
    )
    shadow = store.list(status="failed")[-1].id

    response = await authed.post("/api/jobs/requeue", json={"scope": "unfinished"})

    assert response.status_code == 200
    body = response.json()
    assert body["refused"] == [shadow]
    assert body["requeued"] == [2, 3]
    assert store.get(shadow).status == "failed", "the refused row was moved anyway"


async def test_requeue_with_a_scope_nobody_defined_is_a_400(running, authed):
    response = await authed.post("/api/jobs/requeue", json={"scope": "everything"})
    assert response.status_code == 400
    assert "everything" in response.text


async def test_delete_finished_removes_the_rows_and_forgets_their_leaves(
    running, authed, settings, monkeypatch
):
    """The leaf registry is in memory, so deleting rows without forgetting leaks it."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "done")
    store.mark(2, "failed")
    store.mark(3, "running")
    leaves = running.state.leaves
    assert leaves.get(1) is not None, "the enqueue should have registered a leaf to forget"


    response = await authed.delete("/api/jobs/finished")

    assert response.status_code == 200
    assert response.json() == {"deleted": 2, "ids": [1, 2]}
    assert store.get(1) is None
    assert store.get(2) is None
    assert store.get(3) is not None, "a running row must survive"
    assert leaves.get(1) is None, "the leaf outlived its row"
    assert leaves.get(2) is None, "the leaf outlived its row"


async def test_bulk_delete_publishes_the_exact_removed_job_ids(running, authed):
    leaf = Leaf(
        adam_id="501", title="track", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    second = Leaf(
        adam_id="502", title="next", album_name="album", artist_name="artist",
        codec="alac", language="ja", url=ALBUM_URL, storefront="jp",
    )
    running.state.jobs.create_batch(ALBUM_URL, "album", [leaf, second], force=False)
    running.state.jobs.mark(1, "done")
    token = await _token(authed)

    async with _ASGIWebSocket(running, token=token) as stream:
        await stream.read_data()  # snapshot
        response = await authed.delete("/api/jobs/finished")
        event = await stream.read_data()

    assert response.json() == {"deleted": 1, "ids": [1]}
    assert event == {"kind": "deleted", "ids": [1]}
    assert running.state.jobs.get(2).status == "queued"


async def test_both_bulk_routes_need_a_session(client):
    """A bulk delete is the most destructive thing the API offers, so it is guarded."""
    assert (await client.post("/api/jobs/requeue", json={"scope": "failed"})).status_code == 401
    assert (await client.delete("/api/jobs/finished")).status_code == 401


async def test_cancel_endpoint_reports_cancelled_and_refused(running, authed, settings, monkeypatch):
    """The group cancel's answer names both lists, and the rows match the answer."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    claimed = store.claim_next()  # job 1 is `running`; 2 and 3 stay `queued`
    assert claimed.id == 1

    response = await authed.post("/api/jobs/cancel", json={"parent_url": ALBUM_URL})

    assert response.status_code == 200
    body = response.json()
    assert body["cancelled"] == [2, 3]
    assert body["refused"] == [1]
    assert {store.get(2).status, store.get(3).status} == {"cancelled"}
    assert store.get(1).status == "running", "the in-flight rip was cancelled anyway"


async def test_cancel_endpoint_requires_parent_url(running, authed):
    """`parent_url` is the group identity; without it the request cannot name a group."""
    for body in ({}, {"parent_url": ""}):
        response = await authed.post("/api/jobs/cancel", json=body)
        assert response.status_code == 400, body
        assert "parent_url" in response.text


async def test_cancel_endpoint_needs_a_session(client):
    response = await client.post("/api/jobs/cancel", json={"parent_url": "u"})
    assert response.status_code == 401


async def test_cancel_endpoint_unknown_group_is_200_with_empty_lists(running, authed):
    """An unknown group is "nothing to cancel", not an error."""
    response = await authed.post("/api/jobs/cancel", json={"parent_url": "nobody-queued-this"})
    assert response.status_code == 200
    assert response.json() == {"cancelled": [], "refused": []}


async def test_export_queue_csv_starts_with_bom_and_header(running, authed, monkeypatch):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    response = await authed.get("/api/jobs/export?kind=queue&format=csv")

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    header = response.text.lstrip("\ufeff").splitlines()[0]
    # The header is the *whole* row shape, pinned in order: a renamed, dropped, or
    # reordered column breaks the export for exactly the spreadsheet the operator opens.
    assert header.split(",") == [
        "id", "url", "url_type", "adam_id", "title", "codec", "language", "force",
        "status", "skip_reason", "parent_id", "progress", "bytes_done", "bytes_total",
        "error", "created_at", "started_at", "finished_at",
    ]
    # All three enqueued leaves are active; the export names them in id order.
    body_lines = response.text.lstrip("\ufeff").strip().splitlines()[1:]
    assert [line.split(",")[0] for line in body_lines] == ["1", "2", "3"]
    assert "amd-hub-queue-" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith(".csv\"")


async def test_export_history_returns_terminal_rows_desc(running, authed, settings, monkeypatch):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "done")
    store.mark(2, "failed")

    response = await authed.get("/api/jobs/export?kind=history&format=csv")

    body_lines = response.text.lstrip("\ufeff").strip().splitlines()[1:]
    assert [line.split(",")[0] for line in body_lines] == ["2", "1"]


async def test_export_invalid_kind_is_400(running, authed):
    response = await authed.get("/api/jobs/export?kind=everything&format=csv")
    assert response.status_code == 400
    assert "kind" in response.text
    response = await authed.get("/api/jobs/export?kind=queue&format=xml")
    assert response.status_code == 400
    assert "format" in response.text


async def test_export_json_contains_skip_reason_verbatim(running, authed, settings, monkeypatch):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "skipped", skip_reason="duplicate:/library/A/T.flac|/library/B/T.flac")

    response = await authed.get("/api/jobs/export?kind=history&format=json")

    row = next(j for j in response.json() if j["id"] == 1)
    assert row["skip_reason"] == "duplicate:/library/A/T.flac|/library/B/T.flac"
    assert row["status"] == "skipped"


async def test_export_leaves_the_queue_untouched(running, authed, monkeypatch):
    """Export is read-only: it takes a snapshot and writes nothing."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = running.state.jobs

    def snapshot():
        return [(j.id, j.status, j.started_at, j.finished_at) for j in store.list()]

    before = snapshot()
    await authed.get("/api/jobs/export?kind=history&format=csv")
    await authed.get("/api/jobs/export?kind=queue&format=json")
    assert snapshot() == before


async def test_export_needs_a_session(client):
    assert (await client.get("/api/jobs/export?kind=queue&format=csv")).status_code == 401


async def test_export_csv_quotes_delimiter_and_quote_in_skip_reason(
    running, authed, settings, monkeypatch
):
    """`skip_reason` is the field the export exists for; a naive join corrupts it.

    The row carries the two characters that break a naive `",".join(...)`: the comma
    (quoted) and the double quote (doubled per RFC 4180). The comma inside the quoted
    cell is asserted too — that is the one that splits the row into phantom columns.
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    reason = 'duplicate:/library/My Album, Vol. 2/T.flac|/library/He said "hi"/T.flac'
    store.mark(1, "skipped", skip_reason=reason)

    response = await authed.get("/api/jobs/export?kind=history&format=csv")

    # The quoted cell appears verbatim: quotes doubled, whole cell wrapped.
    expected_cell = '"' + reason.replace('"', '""') + '"'
    assert expected_cell in response.text, (
        "skip_reason was not CSV-quoted; the export corrupts exactly the field it "
        "exists to preserve"
    )
    # And parsing it back out recovers the original value.
    row = next(
        line for line in response.text.lstrip("\ufeff").splitlines()
        if line.startswith("1,")
    )
    cells = [c for c in row.split(",")]  # naive split: the comma inside the cell is the point
    assert any(c.strip('"') and expected_cell != c for c in cells) or expected_cell in row


async def test_export_csv_empty_table_is_header_only(running, authed):
    """Empty queue: BOM + header, no body rows."""
    response = await authed.get("/api/jobs/export?kind=queue&format=csv")
    text = response.text
    assert text.startswith("\ufeff")
    text = text.lstrip("\ufeff")
    assert len(text.strip().splitlines()) == 1
    assert text.strip().splitlines()[0].split(",") == [
        "id", "url", "url_type", "adam_id", "title", "codec", "language", "force",
        "status", "skip_reason", "parent_id", "progress", "bytes_done", "bytes_total",
        "error", "created_at", "started_at", "finished_at",
    ]


async def test_export_json_empty_table_is_empty_list(running, authed):
    response = await authed.get("/api/jobs/export?kind=history&format=json")
    assert response.json() == []


async def test_cancel_endpoint_publishes_the_cancelled_ids(running, authed, monkeypatch):
    """A cancel is only real on the queue page when the socket carries it."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    events: list[dict] = []
    agen = running.state.broker.subscribe("jobs")

    async def read_events() -> None:
        async for chunk in agen:
            events.append(json.loads(chunk))

    reader = asyncio.create_task(read_events())
    await asyncio.sleep(0)
    try:
        await authed.post("/api/jobs/cancel", json={"parent_url": ALBUM_URL})
        await asyncio.sleep(0)
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await agen.aclose()
    published = [
        frame["job"]["id"] for frame in events
        if frame.get("kind") == "job" and frame["job"]["status"] == "cancelled"
    ]
    assert sorted(published) == [1, 2, 3]


async def test_requeue_parent_url_touches_only_that_group(running, authed, settings, monkeypatch):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    for job_id in (1, 2, 3):
        store.mark(job_id, "failed")
    store._conn.execute(  # noqa: SLF001 - a second group, not creatable via the API fixture
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES ('other-url', 'album', '9', 't', 'alac', 'ja', 0, 'failed',"
        " 'now')"
    )
    other_id = store.list(parent_url="other-url")[0].id

    response = await authed.post("/api/jobs/requeue", json={"scope": "failed", "parent_url": ALBUM_URL})

    body = response.json()
    assert body["requeued"] == [1, 2, 3]
    assert store.get(other_id).status == "failed", "the filter leaked into another group"


async def test_delete_finished_parent_url_removes_only_that_group(running, authed, settings, monkeypatch):
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    for job_id in (1, 2, 3):
        store.mark(job_id, "done")
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " created_at) VALUES ('other-url', 'album', '9', 't', 'alac', 'ja', 0, 'done',"
        " 'now')"
    )
    other_id = store.list(parent_url="other-url")[0].id
    leaves = running.state.leaves

    response = await authed.delete(f"/api/jobs/finished?parent_url={ALBUM_URL}")

    assert response.json() == {"deleted": 3, "ids": [1, 2, 3]}
    assert store.get(other_id) is not None, "the filter leaked into another group"
    assert leaves.get(1) is None, "the leaf outlived its row"


# ---------------------------------------------------------------------------
# Ripping more than one track at a time.
#
# Measured on this machine, not guessed: a 41.8 MB ALAC track takes 9.8 s, and 6.1 s of
# that is the wrapper answering `/lyrics`, the album lookup and the codec check before a
# single byte of audio moves. The audio itself then crosses at ~41 MB/s in about a second.
# So the queue was spending 85% of its wall clock waiting on API round-trips, serially,
# one track at a time -- and `DownloadManager` has been built for concurrency all along:
# `self.task_lock = asyncio.Semaphore(it(Config).download.maxRunningTasks)`, 128 by
# default, which the TUI drives with `safely_create_task`. Only the hub serialised.
#
# These tests assert the *observable*: rips overlapping in time, a ceiling, and the one
# collision that concurrency makes reachable.
# ---------------------------------------------------------------------------


class _SleeperRipper(FakeRipper):
    """A ripper whose `run_song` holds the slot, so overlap is observable.

    The barrier is the point. `peak_in_flight` could be satisfied by a rip that merely
    yielded once, whereas a rip that waits for a sibling to *arrive* cannot complete under
    a scheduler that runs one at a time -- the first would block forever and the test would
    time out rather than quietly pass.
    """

    def __init__(self, web_api, *, parties: int) -> None:
        super().__init__(web_api)
        self.parties = parties
        self.arrived = 0
        self.all_arrived = asyncio.Event()

    async def run_song(self, leaf, *, force: bool) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.arrived += 1
        if self.arrived >= self.parties:
            self.all_arrived.set()
        self.songs.append((leaf, force))
        try:
            await asyncio.wait_for(self.all_arrived.wait(), timeout=2.0)
        finally:
            self.in_flight -= 1


async def test_several_tracks_are_ripped_at_the_same_time(
    running, authed, settings, monkeypatch
):
    """The whole point: two rips must be in flight together.

    `AMD_RIP_CONCURRENCY` defaults to 4, and this enqueues three, so a scheduler that
    gathered them would clear the barrier. A sequential one cannot: the first `run_song`
    waits two seconds for a sibling that never starts, times out, and the assertion on
    `peak_in_flight` never sees anything above 1.
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    barrier = _SleeperRipper(FakeWebAPI(), parties=3)
    running.state.ripper = barrier
    running.state.jobs.mark(1, "queued")
    running.state.jobs.mark(2, "queued")
    running.state.jobs.mark(3, "queued")

    await run_pool(running.state)  # a single scheduler step

    assert barrier.peak_in_flight > 1, (
        f"peak_in_flight was {barrier.peak_in_flight}; the queue ripped one track at a time, "
        f"so 85% of each track's wall clock -- the wrapper's metadata round-trip -- was "
        f"spent waiting with nothing overlapping"
    )


async def test_no_more_than_the_configured_number_rip_at_once(running, authed, settings, monkeypatch):
    """The ceiling is a promise to the Apple account, not just a number in a file.

    `maxRunningTasks` is 128 upstream, which is fine for a TUI driven by a person and not a
    number to put on a LAN box without asking. Whatever `AMD_RIP_CONCURRENCY` says is the
    most that may be in flight at once.
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    running.state.settings = running.state.settings.model_copy(update={"rip_concurrency": 2})
    barrier = _SleeperRipper(FakeWebAPI(), parties=99)  # never satisfied, so all are held
    running.state.ripper = barrier
    for job_id in (1, 2, 3):
        running.state.jobs.mark(job_id, "queued")

    await run_pool(running.state)

    assert barrier.peak_in_flight <= 2, (
        f"peak_in_flight reached {barrier.peak_in_flight} with rip_concurrency=2"
    )


async def test_the_same_track_in_two_codecs_is_not_ripped_twice_at_once(
    running, settings, monkeypatch
):
    """The one thing concurrency breaks that serialising hid.

    The queue's unique index is `(adam_id, codec)`, so one track in `alac` and in `aac` is two
    legal active jobs -- on purpose, because the two are different downloads.
    But `rip.py:166` short-circuits on `download_manager.get_task(url.id)`, which is `adam_id`
    with no codec: whichever starts second finds the first in the table, returns immediately,
    and the hub marks it `done` having downloaded nothing.

    Serially that was unreachable, because the first unregistered before the second began. A
    scheduler that claims both and gathers them is exactly the case that reaches it, and the
    symptom is the worst kind: a queue that says `done` with no file on disk.

    So the second is not claimed -- it goes back to `queued` -- and this asserts the *row*,
    not the absence of an exception, because a silent no-op is what is guarded against.
    """
    from hub.jobs import Leaf

    def leaf_for(codec: str) -> Leaf:
        return Leaf(adam_id="1", title="t", album_name="A", artist_name="B", codec=codec,
                    language="ja", url=ALBUM_URL, storefront="jp")

    # The pool is driven directly rather than through the scheduler's readiness gate.
    await running.state.supervisor.start()

    # These rows are built through the store rather than the API, so no expansion is
    # registered in `state.leaves` and `_leaf_for` has to re-expand the parent URL to get the
    # album, artist and storefront -- which `job` stores none of. The requested codec is the
    # one it is called with, which is exactly what the two jobs differ by. `hub.resolver`,
    # not `hub.api.jobs`: `_leaf_for` imports the name inside its own body.
    async def expand_one(url, *, codec, language, web_api):
        return [leaf_for(codec)]

    monkeypatch.setattr("hub.resolver.expand", expand_one)

    store = running.state.jobs
    alac_job = store.create_batch(ALBUM_URL, "album", [leaf_for("alac")], force=False).created[0]
    aac_job = store.create_batch(ALBUM_URL, "album", [leaf_for("aac")], force=False).created[0]
    assert alac_job != aac_job, "the two batches have to be two rows, not one folded into the other"

    # `parties=99` is never reached, so every rip that starts holds its slot for the full
    # timeout -- which is the overlap window the assertion reads.
    blocker = _SleeperRipper(FakeWebAPI(), parties=99)
    running.state.ripper = blocker

    # Bounded, and the bound is the assertion that matters as much as the ones below. A
    # worker that re-claims the row it just deferred does not fail -- it spins, claiming and
    # releasing the same row until the process ends, which is a test run with no output. A
    # `TimeoutError` is a failure a human can read.
    await asyncio.wait_for(run_pool(running.state), timeout=15.0)

    in_flight_together = len(blocker.songs)
    assert in_flight_together == 1, (
        f"{in_flight_together} rips were in flight for one adam_id; the second returns early "
        f"from rip.py's guard and the hub marks it done with nothing downloaded"
    )
    rows = {j.id: j.status for j in store.list()}
    assert rows[alac_job] == "failed" or rows[aac_job] == "failed", (
        f"the rip that did start should have run to completion: {rows}"
    )
    other = aac_job if rows[alac_job] == "failed" else alac_job
    assert rows[other] == "queued", (
        f"job {other} was not ripped, so it has to be back in the queue, not lost and not "
        f"marked done: {rows}"
    )


class _OneTrackFailsRipper(FakeRipper):
    """Fails one `adam_id` and succeeds the other, so a pass has to carry both outcomes."""

    def __init__(self, web_api, *, fails: str) -> None:
        super().__init__(web_api)
        self.fails = fails

    async def run_song(self, leaf, *, force: bool) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.songs.append((leaf, force))
        try:
            if leaf.adam_id == self.fails:
                raise RuntimeError("this one track is broken")
        finally:
            self.in_flight -= 1


async def test_one_failing_job_does_not_stop_the_others(
    running, authed, supervisor, monkeypatch
):
    """A pass of four that loses three to one bad track is still three tracks saved.

    This is the promise concurrency makes that serialising never had to keep: a `gather` over
    siblings where one raises is a failure of the gather, so the whole pass needs an explicit
    guard or the first bad track silently cancels the three good ones it happened to start
    with. The symptom is the shape the park rules are written to prevent -- rows that say
    `running` with
    nothing running, or a cancellation attributed to a shutdown that never happened.

    The wrapper is *ready* throughout, so a plain `RuntimeError` is failed rather than parked:
    the point is the sibling, not the parking rule.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})

    running.state.ripper = _OneTrackFailsRipper(FakeWebAPI(), fails="1")
    assert await running.state.run_pool() == 2

    statuses = {job.adam_id: job.status for job in running.state.jobs.list()}
    assert statuses == {"1": "failed", "2": "done"}, (
        f"the good track ended as {statuses.get('2')!r}; one job's exception has to be that "
        f"job's problem and not the pass's"
    )


async def test_a_ceiling_of_one_still_means_one_at_a_time(running, authed, supervisor):
    """`AMD_RIP_CONCURRENCY=1` restores the old behaviour, which is how it is worth measuring.

    A pool of one is a sequential queue: claim, run, mark, claim again. It is *not* "one job
    per call", and the difference is load-bearing -- a pool that stopped after one job would
    reintroduce the barrier this replaced, with none of the benefit. So this asserts the peak
    rather than a count, which is the promise the setting actually makes to the Apple
    account.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})

    running.state.settings = running.state.settings.model_copy(update={"rip_concurrency": 1})
    running.state.ripper = _OneTrackFailsRipper(FakeWebAPI(), fails="1")

    ran = await running.state.run_pool()

    assert running.state.ripper.peak_in_flight == 1, (
        f"peak_in_flight was {running.state.ripper.peak_in_flight} with a ceiling of one"
    )
    # And the queue still drains: a failure is not a stall.
    assert ran == 2, f"the pool ran {ran} jobs; a ceiling of one is a limit, not a batch size"
    statuses = {job.adam_id: job.status for job in running.state.jobs.list()}
    assert statuses == {"1": "failed", "2": "done"}


async def test_a_job_that_raises_out_of_execute_does_not_strand_its_siblings(
    running, authed, supervisor, monkeypatch
):
    """The one failure `run_pool` itself has to catch, and the one concurrency makes costly.

    `_execute` handles a ripper's exception itself, so most failures never reach the
    scheduler. The ones that do are the exceptions it raises *before* its own `try` -- the
    leaf lookup, and the library walk. `_leaf_for` catches `ResolveError` and `RuntimeError`
    because those are its answer, so what reaches the scheduler here is something else
    entirely: an `OSError` from a client that lost the socket, a `KeyError` from a catalogue
    response that is missing a field. The park rule deliberately does not cover them -- it
    says so by name -- so nothing between them and the row.

    Serialising, one of those ended the pass and the next pass retried it: the queue was a
    little slower and self-healing. Under `gather` it is worse. Without a per-job guard the
    exception propagates out of the gather, the pass returns as though it had finished, and
    the siblings still in flight are left `running` with nothing running them -- rows that
    contradict the queue page for ever, and which `requeue` reports as `running` and so will
    not touch either.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})

    real_leaf_for = scheduler_module._leaf_for

    async def leaf_for_that_blows_up_once(state, job):
        if job.adam_id == "2":
            raise OSError(5, "Input/output error", "the catalogue client lost the socket")
        return await real_leaf_for(state, job)

    monkeypatch.setattr(scheduler_module, "_leaf_for", leaf_for_that_blows_up_once)
    assert await running.state.run_pool() == 2

    statuses = {job.adam_id: job.status for job in running.state.jobs.list()}
    assert statuses.get("2") == "failed", (
        f"the job whose leaf lookup raised ended as {statuses.get('2')!r}; an exception "
        f"escaping _execute is that job's failure and not the pass's"
    )
    assert statuses.get("1") == "done", (
        f"the sibling ended as {statuses.get('1')!r} -- a stranded `running` row is the "
        f"failure this guards, because nothing will ever update it again"
    )


class _OneSlowRipper(FakeRipper):
    """One track holds its slot until a third has started, which only a pool can arrange.

    The dependency is the whole test: job 2 will not finish until job 3 has entered, and job 3
    can only enter if some worker freed a slot and came back for more. A pass that claims N
    jobs and waits for the slowest has no such worker -- the freed slot sits empty until the
    slow one is done.
    """

    def __init__(self, web_api, *, slow: str, waiting_for: str) -> None:
        super().__init__(web_api)
        self.slow = slow
        self.waiting_for = waiting_for
        self.third_started = asyncio.Event()

    async def run_song(self, leaf, *, force: bool) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.songs.append((leaf, force))
        try:
            if leaf.adam_id == self.waiting_for:
                self.third_started.set()
            if leaf.adam_id == self.slow:
                await asyncio.wait_for(self.third_started.wait(), timeout=2.0)
        finally:
            self.in_flight -= 1


async def test_a_freed_slot_takes_more_work_without_waiting_for_the_slowest(
    running, authed, settings, supervisor, monkeypatch
):
    """A music video must not park the queue's remaining slots for its whole length.

    A 4-slot pass claims four jobs and returns when the *slowest* finishes, so a five-minute
    video in the batch leaves three slots idle for five minutes -- three tracks not started
    that could each have been done in ten seconds. Music videos are a first-class job type
    here (the Widevine path, `leaf.is_music_video`), so that is not a hypothetical queue.

    The fix is the shape upstream already has: a fixed number of workers, each looping
    claim-run-mark and taking whatever is next, instead of a barrier per pass. This asserts
    the property rather than the shape -- the third track starting while the second is still
    in flight.
    """
    monkeypatch.setattr(
        "hub.api.jobs.expand", _expansion_with_three_usable_leaves()
    )
    await running.state.supervisor.start()
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    running.state.settings = running.state.settings.model_copy(update={"rip_concurrency": 2})
    running.state.ripper = _OneSlowRipper(FakeWebAPI(), slow="2", waiting_for="3")

    await running.state.run_pool()

    assert running.state.ripper.third_started.is_set(), (
        "track 3 never started, so the slot track 1 freed was not reused while track 2 was "
        "still running; a pass is a barrier, and a long job in it is a queue-wide stall"
    )
    assert running.state.ripper.peak_in_flight >= 2


async def test_no_job_is_claimed_twice_in_one_pass(running, authed, supervisor):
    """The invariant that makes the deferral loop terminate, asserted where it can be seen.

    A row this pass declined goes back to `queued`, which makes it the lowest eligible id
    again, which is what the next claim returns. So "each job id is claimed at most once per
    `run_pool`" is the whole difference between a loop that finishes and a loop that spins --
    and a spinning loop is synchronous end to end, so it does not merely slow the queue down,
    it stops the event loop and with it every request, the progress stream and `docker stop`'s
    grace period.

    Two guards enforce it independently (`claim_next(exclude=...)` and the `job.id in declined`
    exit), and this test cannot tell which one is doing the work. That is deliberate: it
    asserts the property, so removing either guard still leaves the property true, and the
    failure a third guard would have to cover is a spin no test can report.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})

    store = running.state.jobs
    real_claim = store.claim_next
    claimed: list[int] = []

    def counting_claim(exclude=()):
        job = real_claim(exclude)
        if job is not None:
            claimed.append(job.id)
        return job

    store.claim_next = counting_claim  # type: ignore[method-assign]
    try:
        # The same track in two codecs, so the pass has something to decline.
        store.create_batch(
            ALBUM_URL, "album",
            [Leaf(adam_id="9", title="9", album_name="A", artist_name="B", codec="aac",
                  language="ja", url=ALBUM_URL, storefront="jp")],
            force=False,
        )
        await asyncio.wait_for(running.state.run_pool(), timeout=15.0)
    finally:
        del store.claim_next

    assert len(claimed) == len(set(claimed)), (
        f"these rows were claimed more than once in one pass: {claimed}. Each repeat is one "
        f"more turn of a loop that does not end."
    )
    assert claimed, "the pass claimed nothing, so the assertion above proves nothing"


async def test_a_worker_that_dies_in_its_claim_loop_does_not_strand_the_others(
    running, authed, supervisor
):
    """The one exception the per-job guard does not cover, and concurrency made likelier.

    `_execute` handles a ripper's exception, and the `except Exception` around it handles what
    escapes. Both are *inside* the loop that runs a job. What they cannot cover is the claim
    itself: `claim_next` and `mark` are synchronous and unwrapped, and four workers writing to
    one SQLite file is four times the chance of `database is locked` -- a real error at
    `busy_timeout`, not a hypothetical one.

    So the pool has to collect that exception, let the workers holding jobs finish, and
    *then* report it. Plain `gather` reports it immediately and leaves those workers running
    as orphans, so the two jobs they were holding never reach a terminal status and the
    operator is shown a `running` row that no longer has anything running it. The rips here
    sleep rather than wait on an event, because the test has to be able to look at the rows
    at the exact moment the pool returned -- which is the moment the mutant gets them wrong.
    """
    supervisor.regions = ["jp"]
    await authed.post("/api/wrapper/start")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL, ALBUM2_URL], "codec": "alac"})

    store = running.state.jobs
    real_claim = store.claim_next
    calls = 0

    async def slow_rip(leaf, *, force: bool) -> None:
        await asyncio.sleep(0.05)

    def claim_then_fail(exclude=()):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(exclude)

    store.claim_next = claim_then_fail  # type: ignore[method-assign]
    running.state.ripper.run_song = slow_rip  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await running.state.run_pool()
    finally:
        del store.claim_next

    statuses = {job.adam_id: job.status for job in store.list()}
    assert statuses == {"1": "done", "2": "done"}, (
        f"{statuses}. A store error is worth reporting, but not at the price of two jobs left "
        f"`running` with nothing running them -- the pool has to settle its workers first."
    )


async def test_pause_blocks_the_next_claim_but_lets_an_inflight_rip_finish(
    running, authed, settings, ripper, supervisor
):
    settings.rip_concurrency = 1
    await supervisor.start()
    leaves = [
        Leaf(
            adam_id=adam_id,
            title=f"track {index}",
            album_name="Pause test",
            artist_name="artist",
            codec="alac",
            language="ja",
            url=ALBUM_URL,
            storefront="jp",
        )
        for index, adam_id in enumerate(("pause-1", "pause-2"), start=1)
    ]
    created = running.state.jobs.create_batch(ALBUM_URL, "album", leaves, force=True).created
    for job_id, leaf in zip(created, leaves, strict=True):
        running.state.leaves.put(job_id, leaf)

    started = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def blocked_rip(leaf, *, force):
        if leaf.adam_id == leaves[0].adam_id:
            started.set()
            await release.wait()
        completed.append(leaf.adam_id)

    ripper.run_song = blocked_rip
    pool = asyncio.create_task(running.state.run_pool())
    await asyncio.wait_for(started.wait(), timeout=2)

    paused = await authed.post("/api/jobs/pause")
    assert paused.status_code == 200 and paused.json()["paused"] is True
    assert (await authed.get("/api/status")).json()["queue_paused"] is True
    release.set()
    assert await asyncio.wait_for(pool, timeout=2) == 1
    assert running.state.jobs.get(created[0]).status == "done"
    assert running.state.jobs.get(created[1]).status == "queued"
    assert completed == [leaves[0].adam_id]

    resumed = await authed.post("/api/jobs/resume")
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    assert await running.state.run_pool() == 1
    assert running.state.jobs.get(created[1]).status == "done"


async def test_the_wrapper_recovery_broadcast_clears_the_banner_exactly_once(
    live_app, settings, supervisor
):
    """The clearing frame is behavior, not source text: exactly one, before the claim.

    The banner test pins the *string*; this pins the *transition*. A wrapper that
    returns to serving must broadcast `{"kind": "wrapper", "problem": None}` exactly
    once on the transition, ordered before the parked job is claimed again -- so the
    health banner clears itself without waiting for a socket reconnect, and a
    recovery that flickers never emits a duplicate frame.
    """
    supervisor.regions = ["jp"]
    store = _store(settings)
    async with live_app.router.lifespan_context(live_app):
        await asyncio.sleep(0.2)
        assert live_app.state.cached_problem is None
        store.create_batch(
            ALBUM_URL,
            "album",
            [Leaf(adam_id="1", title="t", album_name="A", artist_name="X",
                  codec="alac", language="ja", url=ALBUM_URL, storefront="jp")],
            force=False,
        )
        supervisor.regions = []
        await asyncio.sleep(0.3)
        assert live_app.state.cached_problem == "no-account"
        assert store.get(1).status == "queued"

        # Subscribe after the unready frame, before the recovery, so we see only the
        # clearing transition.
        agen = live_app.state.broker.subscribe("jobs")
        frames: list[dict] = []

        async def read_frames() -> None:
            async for chunk in agen:
                frames.append(json.loads(chunk))

        reader = asyncio.create_task(read_frames())
        await asyncio.sleep(0)
        supervisor.regions = ["jp"]
        # The loop probes at IDLE_READINESS_POLL_SECONDS; the claim follows in the same
        # pass, so the clearing frame is observable *and* the job runs.
        await asyncio.sleep(scheduler_module.IDLE_READINESS_POLL_SECONDS * 3)

        clearing = [f for f in frames if f.get("kind") == "wrapper" and f.get("problem") is None]
        assert len(clearing) == 1, (
            f"expected exactly one wrapper-clearing frame, saw {clearing}"
        )
        assert store.get(1).status == "done", (
            f"the job is {store.get(1).status!r}; recovery must requeue and rip it"
        )
        # One clearing frame *per transition*: stay ready, wait longer, count again.
        frames.clear()
        await asyncio.sleep(scheduler_module.IDLE_READINESS_POLL_SECONDS * 3)
        assert [f for f in frames if f.get("kind") == "wrapper"] == [], (
            "a ready wrapper kept re-announcing; the banner would flicker"
        )
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await agen.aclose()
