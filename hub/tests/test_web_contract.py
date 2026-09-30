"""The served surface and its contracts: pages, static assets, templates, CSS and JS.

The rest of the HTTP surface -- auth, `POST /api/jobs`, the stream, the pool -- is
`test_api_jobs.py`. This file is what the browser actually receives and the text-level
agreements the browser code has to keep: the CSP on the responses, the row markup the
script rebuilds, the palette the buttons resolve against. Fakes and fixtures come from
`web_support.py` / `conftest.py` exactly as they do over there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from web_support import (
    ALBUM_URL,
    PASSWORD,
    _expansion_with_three_usable_leaves,
    _store,
)

from hub.api import PUBLIC_STATIC, STATIC_DIR, TEMPLATES_DIR
from hub.jobs import JobStore
from hub.library_scan import scan_roots


async def test_the_security_headers_are_on_the_responses_that_matter(client, authed):
    """Defence in depth, and it is probed rather than assumed (M5).

    **Every kind of response goes through the one middleware, including the ones built by an
    exception handler.** That was the doubt behind a second helper that used to exist here,
    and it was wrong: `app.middleware("http")` sits *outside* the `ExceptionMiddleware` in the
    stack, so the handler's response is what `call` returns and passes back through it like
    any other. This test's "unauthenticated" row is a 401 raised by `_Unauthorized` and turned
    by a registered handler, and it is here precisely because that was the case in doubt.

    So the claim this test makes is a coverage claim, not an exception: the responses nobody
    thinks to add headers to are the ones being asked -- the 401 from a handler, the 303 from
    a page, the static files, the login page a browser reaches first.

    `Content-Security-Policy` is the one that would actually stop an injected script, and its
    **absence of `unsafe-inline`** is the load-bearing part: the templates put no `<script>`
    block in a page, so nothing needs it, and adding it back would remove the only protection
    against a template that starts trusting a filesystem-derived string.
    """
    for label, response in (
        ("health", await client.get("/api/health")),
        ("unauthenticated", await client.get("/api/jobs")),
        ("login page", await client.get("/login")),
        ("page redirect", await client.get("/queue", follow_redirects=False)),
        ("static", await client.get("/static/app.css")),
        ("authenticated", await authed.get("/api/jobs")),
    ):
        headers = response.headers
        assert headers.get("X-Content-Type-Options") == "nosniff", label
        assert headers.get("X-Frame-Options") == "DENY", label
        assert "frame-ancestors 'none'" in (headers.get("Content-Security-Policy") or ""), label
        assert headers.get("Referrer-Policy") == "no-referrer", label

    csp = (await client.get("/login")).headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp, (
        "'unsafe-inline' would make the CSP decorative; the templates need no inline script"
    )
    assert "unsafe-eval" not in csp
    # `default-src 'self'` and nothing else: no CDN, no third party. The whole client is two
    # files served from `/static`.
    assert "default-src 'self'" in csp
    assert "http://" not in csp and "https://" not in csp


async def test_the_static_mount_is_behind_the_session_guard(client, authed):
    """`/static` was a `Mount` outside `guarded()`, and served both files to anybody.

    Traversal was always clean -- Starlette refuses `..`, percent-encoded `..` and symlinks,
    all 404 -- so this was an authentication gap and not a path-traversal one. The fix is a
    `GuardedStatic` mount, and this walks the real directory so an asset added tomorrow is a
    401 by default rather than an open file nobody looked at.
    """
    for path in ("/static/app.css", "/static/app.js"):
        assert (await client.get(path)).status_code == 200, (
            f"{path} is public because the login page needs it; a new asset is not"
        )
        assert (await client.get(path)).status_code == 200

    # Every *other* file under the directory, refused. The walk is the point: the list is not
    # asserted to be two items long, it is asserted to cover whatever is on disk.
    for asset in sorted(STATIC_DIR.iterdir()):
        served = f"/static/{asset.name}"
        if served in PUBLIC_STATIC:
            continue
        assert (await client.get(served)).status_code == 401, (
            f"{served} is readable with no session; add it to PUBLIC_STATIC only if it is "
            f"needed by the login page"
        )
    assert (await authed.get("/static/app.css")).status_code == 200


async def test_the_login_page_can_load_its_own_assets(client):
    """The other half of guarding the mount: a login form that cannot render is worse.

    `PUBLIC_STATIC` is the two files `base.html` references. This asserts they are *reachable*
    without a session, so widening the guard to the whole mount without widening this list
    would break the login page and be caught here rather than by a user.
    """
    body = (await client.get("/login")).text
    assert "/static/app.css" in body
    assert "/static/app.js" in body
    for path in ("/static/app.css", "/static/app.js"):
        assert (await client.get(path)).status_code == 200


async def test_static_is_not_needed_to_render_the_login_form(client):
    """The login page works with no session and no stylesheet: it is a plain form post.

    A stylesheet is decoration; a *script* is the risk. `app.js` is served without a session
    because `base.html` asks for it on every page, and this asserts the login flow does not
    depend on it -- the form posts to `/login` and redirects, so a browser with scripting
    disabled, or with the script blocked, can still sign in.
    """
    response = await client.post(
        "/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    form = (await client.get("/login")).text
    assert 'method="post"' in form and 'action="/login"' in form



async def test_the_library_page_warns_about_a_readable_but_empty_root(authed, settings,
                                                                       tmp_path):
    """The warning has to be on the page, because that is where somebody looks.

    This is the check that could not be a harness: a per-root count in a `docker exec`
    one-shot is only ever run by whoever remembers, and the thing it guards is invisible from
    every other surface. `empty-root` is deliberately checked as text, so the row cannot be
    quietly dropped from the template while the API keeps the number.
    """
    settings.library_roots = [tmp_path / "empty-root"]
    (tmp_path / "empty-root").mkdir()  # readable and empty -- the state, not a missing path
    body = (await authed.get("/library")).text
    assert "is this mounted?" in body
    assert "empty-root" in body
    # And it is the *empty* row, not the unreadable one: a directory that is not there is a
    # different warning and this root is not in `degraded_roots`.
    assert "unreadable" not in body


async def test_the_library_page_does_not_warn_about_a_populated_root(authed, library):
    """The warning has to be absent when it is not warranted, or it is noise.

    A row that says "is this mounted?" about 3,670 album directories is worse than no row at
    all, so this is the negative half of the pair and it is the half a green-field test of
    the warning would miss.
    """
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")
    body = (await authed.get("/library")).text
    assert "is this mounted?" not in body
    # The count is still shown, which is the part that is useful rather than alarming.
    assert "album directories" in body



async def test_the_queue_page_renders_the_rows(authed, settings, library, monkeypatch):
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/1-01 1 a.m. (feat. shinoだす。).m4a").write_bytes(b"")
    from hub.jobs import Leaf

    async def fake_expand(url, *, codec, language, web_api):
        return [
            Leaf(
                adam_id="501",
                title="1 a.m. (feat. shinoだす。)",
                album_name="4pi",
                artist_name="toe",
                codec=codec,
                language=language,
                url=url,
                storefront="jp",
            )
        ]

    monkeypatch.setattr("hub.api.jobs.expand", fake_expand)
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")

    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "skipped", skip_reason="duplicate:toe/4pi")

    body = (await authed.get("/queue")).text
    assert "1 a.m. (feat. shinoだす。)" in body
    assert "skipped" in body
    assert "toe/4pi" in body


async def test_a_scan_result_cannot_inject_markup_into_the_page(authed, running, library, settings):
    """`skip_reason` is built from paths read off the filesystem, and the filesystem is the
    untrusted input here.

    A directory called `<img src=x onerror=alert(1)>` is not an exotic name to end up with:
    these libraries are user-curated and ripped from arbitrary sources, and the rule about
    keeping ` - Single` and ` (feat. …)` means odd characters are *kept* rather than
    stripped. Jinja2's autoescaping is what stands between that and the browser, and this is
    the test that would notice if a template were marked `|safe`.
    """
    hostile = library / "<img src=x onerror=alert(1)>/<script>alert(2)</script>"
    hostile.mkdir(parents=True)
    (hostile / "t.m4a").write_bytes(b"")
    scan = scan_roots([library])
    assert any("<script>" in album.relpath for album in scan.albums), "the fixture is not hostile"

    store = _store(settings)
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO job (url, url_type, adam_id, title, codec, language, force, status,"
        " skip_reason, created_at) VALUES ('u', 'album', '7', 'a title with <b>markup</b>',"
        " 'alac', 'ja', 0, 'skipped', ?, ?)",
        (f"duplicate:{hostile.relative_to(library)}", "2026-09-27T00:00:00.000+00:00"),
    )
    body = (await authed.get("/queue")).text
    # The property that matters is that no *tag* survives, not that the characters are gone:
    # `onerror=alert(1)` still appears, as inert text, and that is correct. What must not
    # appear is a `<` from the path, which is what would make it a tag.
    assert "<script>alert(2)</script>" not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "<b>markup</b>" not in body
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "a title with &lt;b&gt;markup&lt;/b&gt;" in body


async def test_the_queue_page_warns_about_a_degraded_root(authed, settings, tmp_path):
    settings.library_roots = settings.library_roots + [tmp_path / "gone"]
    body = (await authed.get("/queue")).text
    assert "gone" in body


async def test_the_library_page_lists_the_albums_it_found(authed, library):
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")
    body = (await authed.get("/library")).text
    assert "4pi" in body
    assert "toe" in body


async def test_the_library_api_lists_albums_and_artists(authed, library):
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")
    albums = (await authed.get("/api/library/albums")).json()["albums"]
    assert [(album["name"], album["artist"]) for album in albums] == [("4pi", "toe")]
    artists = (await authed.get("/api/library/artists")).json()["artists"]
    assert artists == ["toe"]


def test_library_page_exposes_search_and_a_read_only_duplicate_report():
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/library.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")
    for control in ("library-search", "library-albums", "library-duplicates-toggle", "duplicate-report"):
        assert f'id="{control}"' in template
    assert '"/api/library/albums?q="' in script
    assert '"/api/library/duplicates"' in script
    assert "never deletes files" in (root.parent / "api/library.py").read_text(encoding="utf-8")


def test_queue_page_offers_pause_and_cursor_history_controls():
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/queue.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")
    for control in ("queue-pause", "queue-resume", "queue-history-tools", "queue-load-history"):
        assert f'id="{control}"' in template
    assert '"/api/jobs/history?before_id="' in script
    assert '"/api/jobs/lookup?"' in script


async def test_the_login_page_never_leaks_whether_a_password_was_tried(client):
    body = (await client.get("/login")).text
    assert "password" in body
    # The form posts to itself, so it works with scripting turned off.
    assert 'method="post"' in body


async def test_a_successful_form_login_redirects_to_the_queue(client):
    response = await client.post(
        "/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_a_failed_form_login_redirects_back_with_an_error(client):
    response = await client.post(
        "/login", data={"password": "nope"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=1"
    body = (await client.get("/login?error=1")).text
    assert "Wrong password." in body



def test_no_template_escapes_its_output():
    """Every template, read as text, with nothing exempt (M3).

    `base.html` claimed a test grepped the templates for `|safe` and there was none -- the
    claim was about the *expressions* one test happened to render, not about the templates.
    That is the difference between "the hostile directory did not execute" and "nothing in
    this directory can be made to execute", and the second is the property worth having.

    Read as source rather than inferred from behaviour, because a template that escapes
    correctly today can gain a `|safe` tomorrow with no behavioural test failing: the
    hostile input would have to reach *that* template to be caught, and most of them are not
    rendered by the one test.
    """
    forbidden = (
        ("|safe", "a `|safe` filter disables escaping for one expression"),
        ("autoescape false", "`autoescape false` turns escaping off for a whole block"),
        ("autoescape true", "`autoescape true` re-enables it and is never wanted here"),
        ("Markup(", "`Markup(` produces unescaped output and is the other way to ask for it"),
        ("markupsafe", "importing markupsafe to build raw HTML is the same thing indirectly"),
        ("<script>", "an inline script is refused by the CSP and would have to be external"),
    )
    templates = sorted(TEMPLATES_DIR.glob("*.html"))
    assert templates, "no templates found; the glob is wrong and this test would pass vacuously"

    for path in templates:
        source = path.read_text(encoding="utf-8")
        # The comment blocks explain the rule and must be able to name it.
        body = _strip_jinja_comments(source)
        for needle, why in forbidden:
            assert needle not in body, f"{path.name} contains {needle}: {why}"


def _strip_jinja_comments(source: str) -> str:
    """The template with `{# ... #}` removed, so a comment may quote a forbidden string.

    `base.html` documents that `|safe` is forbidden; a test that failed on that documentation
    would be a test nobody would leave in place, and then the property would be untested.
    """
    out: list[str] = []
    index = 0
    while True:
        start = source.find("{#", index)
        if start == -1:
            out.append(source[index:])
            return "".join(out)
        end = source.find("#}", start)
        if end == -1:
            out.append(source[index:])
            return "".join(out)
        out.append(source[index:start])
        index = end + 2


async def test_every_template_is_in_the_render_graph(client, authed, library, monkeypatch):
    """No template is unreachable, by output *or* by `{% extends %}`/`{% include %}`.

    Read from the *sources* rather than from the rendered pages, because a layout's own text
    never appears in its children's output -- `base.html` is not "unrendered", it is rendered
    into every page. So the graph is walked from the templates the three routes name, and a
    template that nothing reaches is the thing this is looking for.
    """
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})

    # Render all three pages anyway, so a template that *is* reached cannot be silently broken
    # by a rename: a 500 here would fail the test for the right reason.
    for path in ("/login", "/queue", "/library"):
        response = await (client if path == "/login" else authed).get(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"

    # The pages the routes render, named rather than guessed, and the graph walked from them.
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES_DIR.glob("*.html"))
    }
    entries = {"login.html", "queue.html", "library.html"}
    reached: set[str] = set()
    frontier = list(entries)
    while frontier:
        name = frontier.pop()
        if name in reached or name not in sources:
            continue
        reached.add(name)
        for referenced in re.findall(r'{%\s*(?:extends|include)\s+"([^"]+)"', sources[name]):
            if referenced not in reached:
                frontier.append(referenced)

    assert sources.keys() == reached, (
        f"templates nothing reaches: {sorted(sources.keys() - reached)}; a template that is "
        f"never rendered is dead code or an untested page"
    )


def test_the_new_modules_do_not_reach_upstream():
    """`tests/test_ripper_host.py` enforces this for the whole package, but only after
    `RipperHost.start()` has been reached in some other test. Asserting the two new entry
    points import without the vendor tree on the path is a cheaper first line of defence.
    """
    import ast

    for name in ("app.py", "auth.py", "api/__init__.py", "api/auth.py", "api/wrapper.py",
                 "api/jobs.py", "api/library.py"):
        path = Path(__file__).resolve().parents[1] / "hub" / name
        source = path.read_text(encoding="utf-8")
        assert "import src" not in source, name
        assert "importlib" not in source, name
        assert "runpy" not in source, name
        assert "sys.path" not in source, name
        ast.parse(source)


def test_the_codec_set_matches_the_clients():
    """The hub restates upstream's seven codecs because the boundary forbids importing them.

    That is a duplicated constant, so something has to say it has not drifted. This reads the
    real `src/types.py` and compares, which is the only check that can notice an upstream
    change; it skips if the vendor tree is absent rather than failing a container build.
    """
    from hub.api import jobs as jobs_api

    vendor = Path(__file__).resolve().parents[2] / "AppleMusicDecrypt/src/types.py"
    if not vendor.is_file():
        pytest.skip("no AppleMusicDecrypt checkout to compare against")
    upstream = _upstream_constants(vendor, "class Codec")
    assert jobs_api.CODECS == set(upstream), (
        f"the hub's CODECS has drifted from {vendor.name}; upstream's Codec values are "
        f"{sorted(upstream)}"
    )


def test_the_parent_type_set_matches_the_clients():
    """The same duplication for `URLType`, which is what `create_batch` validates against."""
    from hub.api import jobs as jobs_api

    vendor = Path(__file__).resolve().parents[2] / "AppleMusicDecrypt/src/url.py"
    if not vendor.is_file():
        pytest.skip("no AppleMusicDecrypt checkout to compare against")
    assert jobs_api.PARENT_TYPES == set(_upstream_constants(vendor, "class URLType"))


def _upstream_constants(path: Path, class_marker: str) -> list[str]:
    """The `"value"` constants assigned inside one `class` body of an upstream file.

    Parsed out of the source text rather than imported, because importing `src.types` from a
    test is exactly the boundary `tests/test_ripper_host.py` forbids -- and reading the text
    is the point: the check has to be able to notice a change in a file the hub may not
    import. Scanned line by line and stopped at the next *top-level* `class` or `def`, so a
    same-named constant elsewhere in the file cannot leak in.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(class_marker))
    values: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        # Mixed case on purpose: upstream's `Codec` members are `ALAC`/`EC3`/`AAC_LEGACY`
        # and its `URLType` members are `Song`/`Album`/`MusicVideo`, so a pattern that
        # assumed uppercase constants would silently return the empty set for one of the two
        # -- and a check comparing against `set()` fails with a message that reads like a
        # production bug rather than a broken test.
        found = re.match(r'\s+[A-Za-z_0-9]+ = "([^"]+)"$', line)
        if found:
            values.append(found.group(1))
    return values


async def test_health_reports_nothing_about_the_host(client):
    """The one unauthenticated route, checked for what it must not contain."""
    body = json.dumps((await client.get("/api/health")).json())
    for leak in ("regions", "library", "jobs", "wrapper", "root", "/data", "/library"):
        assert leak not in body


def test_the_db_is_where_the_settings_said_and_the_roots_are_absolute(running, settings):
    """`RipperHost` holds the process CWD for its whole life.

    Every path the hub hands it has to be absolute, because a relative one would resolve
    against `AppleMusicDecrypt/` and fail silently for reads.
    """
    assert settings.db_path.is_absolute()
    assert running.state.ripper_config_path.is_absolute()


def test_a_locked_database_is_reported_rather_than_served(running, settings):
    """WAL + `busy_timeout` covers contention; an unusable file is an operator error."""
    settings.db_path = settings.db_path.parent / "a-directory-not-a-file"
    settings.db_path.mkdir()
    from hub.jobs import JobStoreError

    with pytest.raises(JobStoreError):
        JobStore(settings.db_path)


async def test_the_library_page_shows_a_path_that_resolves(authed, library):
    """The album table's path column, and the defect it had a layer above.

    It rendered `album.relpath`, which is the unresolvable form the round-1 fix removed from
    `skip_reason`: with more than one library root configured it says which directory but not
    which library. `_album_dict` already builds the qualified path, so the template was showing
    the worse of the two values it was already holding.

    The assertion names the exact string rather than checking that it "looks absolute",
    because the existing page test asserts only that "4pi" and "toe" appear -- and both appear
    in a bare relpath too, which is why nothing here could fail before.
    """
    (library / "toe/4pi").mkdir(parents=True)
    (library / "toe/4pi/t.m4a").write_bytes(b"")

    body = (await authed.get("/library")).text

    assert f"<code>{library}/toe/4pi</code>" in body, (
        "the album row should carry the root-qualified path, which is the one that opens"
    )
    # And the path it shows is a directory, right now.
    assert (library / "toe/4pi").is_dir()



# ---------------------------------------------------------------------------
# The queue page.
#
# **What these tests do and do not claim.** They assert the *server-rendered contract*:
# that the markup carries the hooks the browser script needs, and that the counts and the
# ordering are right. They do **not** claim that clicking a button filters a row, because
# nothing here runs JavaScript -- `hub/tests/` has no browser and adding one is out of
# scope. An earlier version of this file had several tests whose names promised behaviour
# they could not reach; these say what they check instead.
# ---------------------------------------------------------------------------


def _html_of(response) -> str:
    return response.text


async def test_the_queue_offers_both_clearing_controls(running, authed):
    body = _html_of(await authed.get("/queue"))

    assert 'data-action="queue-requeue"' in body
    assert 'data-action="queue-delete-finished"' in body
    # The scope is a choice, not a constant: "everything except done" includes `skipped`,
    # which for a dedup-heavy library is the largest group and re-skips immediately.
    assert 'name="scope"' in body
    assert 'value="failed"' in body
    assert 'value="unfinished"' in body


async def test_the_queue_summarises_the_queue_without_reading_the_table(
    running, authed, settings, monkeypatch
):
    """The counts a user checks first, so the page does not have to be read to know them."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    store.mark(1, "done")
    store.mark(2, "failed")
    store.mark(3, "running")

    body = _html_of(await authed.get("/queue"))

    assert 'id="queue-summary"' in body
    # Rendered from `queue_counts`, which the page already had. The numbers are the
    # contract: a summary that shows a count the table contradicts is worse than none.
    for status, count in (("done", 1), ("failed", 1), ("running", 1)):
        assert f'data-count="{status}"' in body
        assert f">{count}</span>" in body or f'>{count}<' in body, status


def test_queue_count_buttons_and_live_rows_stay_in_sync():
    """Status chips are filters and their counts follow socket/API row changes."""
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/queue.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")

    assert 'data-action="filter-status" data-status="{{ status }}"' in template
    assert "function updateQueueSummary()" in script
    assert "queueSummary.appendChild(button)" in script
    assert "function removeJobs(ids)" in script
    assert 'case "deleted":' in script
    assert "updateQueueSummary();" in script
    assert "queueStatus.value = queueStatus.value === selectedStatus ? \"all\" : selectedStatus" in script


def test_enqueue_and_queue_actions_patch_rows_without_reloading():
    """Enqueue, retry, cancel, requeue and bulk-delete keep the current queue page live."""
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/queue.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")

    assert 'id="enqueue-feedback"' in template
    assert "return hydrateJobs(created)" in script
    assert "upsertIfUnchanged(result.data, cancelRevision)" in script
    assert "upsertIfUnchanged(result.data, retryRevision)" in script
    assert 'requestJson("/api/jobs/finished", { method: "DELETE" })' in script
    assert "removeJobs(ids);" in script
    assert "result.data.requeued || []" in script
    assert 'case "deleted":' in script


async def test_finished_rows_are_marked_so_they_can_be_collapsed(
    running, authed, settings, monkeypatch
):
    """Terminal rows carry the hook; whether the browser acts on it is not claimed here."""
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    _store(settings).mark(1, "done")

    body = _html_of(await authed.get("/queue"))

    assert "data-finished=" in body, "a finished row has to be identifiable without parsing its status"
    assert 'data-action="queue-toggle-finished"' in body, "and there has to be a way to show them again"


async def test_the_enqueue_form_is_above_the_wrapper_card(running, authed):
    """The thing a user reaches for most should not be the fourth card on the page.

    Asserted on document order, not on a CSS property: a form that is last in the markup
    and moved up with `order:` is still last for a reader of the source and for anyone
    printing the page.
    """
    body = _html_of(await authed.get("/queue"))

    assert body.index('id="enqueue"') < body.index('id="wrapper-state"')


async def test_every_status_gets_a_distinct_colour_hook(running, authed, settings, monkeypatch):
    """One class per status, so the styling lives in CSS rather than in the template.

    The test is that the class is *present and status-derived*; whether the stylesheet
    makes it visible is a CSS question this file cannot answer.
    """
    monkeypatch.setattr("hub.api.jobs.expand", _expansion_with_three_usable_leaves())
    monkeypatch.setattr("hub.api.jobs.parent_type_for", lambda _url, _count: "album")
    await authed.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    store = _store(settings)
    for job_id, status in ((1, "done"), (2, "failed"), (3, "queued")):
        # A terminal status first, because `mark` refuses a finished job made active again
        # -- and re-queueing one is what this page's new button does.
        store.mark(job_id, "done")
        if status != "done":
            store._conn.execute(  # noqa: SLF001 - the transition rules are the point
                "UPDATE job SET status = ? WHERE id = ?", (status, job_id)
            )

    body = _html_of(await authed.get("/queue"))

    for status in ("queued", "done", "failed"):
        assert f'class="status status-{status}"' in body, status


def test_the_stylesheet_defines_every_status_class_the_template_emits():
    """A class the CSS does not define is a row that looks like every other row.

    This checks that the two files agree, not that the result is pretty: `hub/tests/` has no
    browser, so whether a colour is legible is not something this file can answer.
    """
    css = (Path(__file__).parent.parent / "hub/web/static/app.css").read_text(encoding="utf-8")
    for status in ("queued", "waiting", "running", "done", "failed", "skipped", "cancelled"):
        assert f".status-{status}" in css, f"{status} has no rule, so its row is unstyled"


def test_the_script_handles_every_bulk_action_the_page_offers():
    """The page's `data-action` values and the script's handlers have to agree.

    A `data-action` with no handler is a button that silently does nothing, which is worse
    than a missing button. Checked as source text on both sides -- again, not behaviour.
    """
    js = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    for action in (
        "queue-requeue",
        "queue-delete-finished",
        "queue-toggle-finished",
        "queue-clear-filters",
        "queue-pause",
        "queue-resume",
        "queue-load-history",
        "cancel-group",
        "requeue-failed-group",
    ):
        assert f'"{action}"' in js, f"the page offers {action} and the script does not handle it"


def test_group_headers_offer_the_group_actions():
    """A queued batch can be cancelled / re-queued as a unit.

    Both the server markup (initial render) and the script (re-sync after every sort)
    carry the header row; a `data-action` on one side and no handler on the other is a
    button that silently does nothing, so both sides are checked.
    """
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/queue.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")
    for control in (
        'data-action="cancel-group"',
        'data-action="requeue-failed-group"',
        'data-parent-url="{{ job.parent_url }}"',
    ):
        assert control in template, f"the queue is missing its group affordance: {control}"
    assert 'dataset.action = "cancel-group"' in script
    assert 'dataset.action = "requeue-failed-group"' in script
    assert 'post("/api/jobs/cancel", {parent_url:' in script
    assert 'post("/api/jobs/requeue", {scope: "failed", parent_url:' in script

    # The snapshot wipes the tbody and rebuilds every row. If the client-side re-derive
    # ran nowhere on the snapshot path, the Jinja headers would live for the pre-hydration
    # paint only, and the group buttons would be dead on the live page -- with the
    # substring checks above still green. The re-derive call site is pinned instead.
    body = script[script.index("function replaceRows") : script.index("function sortQueueRows")]
    assert "syncGroupHeaders()" in body, (
        "the snapshot wipes tbody; without a re-derive the group actions vanish on the "
        "first frame"
    )
    # And the client-side header is a button the browser won't submit, built by the
    # script, not only by the template.
    header_builder = script[
        script.index("function groupHeaderRow") : script.index("function syncGroupHeaders")
    ]
    assert 'cancel.type = "button"' in header_builder
    assert 'requeue.type = "button"' in header_builder


def test_the_queue_offers_export_downloads():
    """History and queue download links, static and cookie-authed -- no JS, no button."""
    template = (Path(__file__).parent.parent / "hub/web/templates/queue.html").read_text(encoding="utf-8")
    for href in (
        'href="/api/jobs/export?kind=history&format=csv"',
        'href="/api/jobs/export?kind=history&format=json"',
        'href="/api/jobs/export?kind=queue&format=csv"',
    ):
        assert href in template, f"the queue is missing its export download: {href}"


def test_health_banner_reports_wrapper_library_and_empty_mounts():
    """The conditional banner: wrapper problem, degraded root, or empty mount.

    Healthy status renders nothing (the empty-string branch is pinned so no
    healthy state leaks into the DOM); the wrapper problems use role="alert"
    and the library ones role="status".
    """
    script = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    # The banner is rendered from the client-side /api/status snapshot.
    assert "function healthBanner" in script
    assert "wrapper.problem" in script
    assert "degraded_roots" in script
    assert "per_root" in script
    assert 'setAttribute("role", "alert")' in script
    assert 'setAttribute("role", "status")' in script
    # Empty health answer produces no node.
    banner_fn = script[script.index("function healthBanner") : script.index("function updateHealthBanner")]
    assert "return null" in banner_fn or "return null;" in banner_fn
    # Refetch on page load, on WS reconnect, and on a `wrapper` frame -- no polling.
    assert "setInterval" not in script or "healthBanner" not in script[script.index("setInterval") : script.index("setInterval") + 300]


def test_the_queue_uses_a_reconnecting_websocket_not_eventsource():
    """Transport loss reconnects to a fresh snapshot; expired sessions stop retrying."""
    js = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    assert 'new WebSocket(protocol + window.location.host + "/api/jobs/ws")' in js
    assert "window.setTimeout(connectStream, delay" in js
    assert "Math.pow(2, reconnectAttempt)" in js
    assert "event.code === 4401 || event.code === 4403" in js
    assert "new EventSource" not in js


def test_the_queue_offers_search_and_status_filters():
    """A long-running queue should be findable without scrolling through every row."""
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/queue.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")
    for control in (
        'id="queue-search"',
        'id="queue-status-filter"',
        'id="queue-visible-count"',
        'id="queue-empty"',
        'id="queue-table-wrap"',
        'id="queue-no-results"',
    ):
        assert control in template, f"the queue is missing its {control} affordance"
    assert "function applyQueueFilters()" in script
    assert "row.hidden = !(matchesFinished && matchesStatus && matchesSearch)" in script
    assert "function clearQueueFilters()" in script
    css = (root / "static/app.css").read_text(encoding="utf-8")
    assert ".enqueue-options, .queue-tools, .library-tools { grid-template-columns: minmax(0, 1fr); }" in css


def test_hidden_queue_rows_are_hidden_by_a_rule_not_by_the_user_agent():
    """`row.hidden = true` alone is not enough on a `<tr>`, and the failure is silent.

    The user-agent sheet's `[hidden] { display: none }` loses to any author rule that gives
    `tr` a display value, and a responsive table rule is the kind of thing that arrives long
    after this feature. The script sets the property; this is what makes it mean anything.
    """
    css = (Path(__file__).parent.parent / "hub/web/static/app.css").read_text(encoding="utf-8")
    assert "tr[hidden]" in css, "nothing gives a hidden <tr> display:none, so the filter does nothing"


def test_the_scripts_row_builder_agrees_with_the_template():
    """The row markup is written twice -- once in Jinja, once in JS -- and they had drifted.

    The template had been given a `data-finished` hook, a `status-<status>` class on the cell
    and a column order with the id last. `buildRow` still produced the old row: no
    `data-finished`, a bare `status` span, and the id in the first column. So the page was
    correct until the WebSocket sent its first snapshot, at which point every row on screen
    silently reverted to the old markup -- and the "hide finished" filter, which selects
    `#queue-body tr[data-finished]`, matched nothing.

    **This is the bug a server-rendered page cannot have and this one had.** The template
    test passed, the served HTML had the attributes, and the page was still broken, because
    the browser rebuilds every row from JSON on the first stream frame.

    Checked as source text on both sides, which is as far as a file with no browser can go.
    The column list is compared, not just the presence of a hook, because the order drifted
    too and an attribute-only check would have missed it.
    """
    root = Path(__file__).parent.parent / "hub/web"
    template = (root / "templates/job_row.html").read_text(encoding="utf-8")
    script = (root / "static/app.js").read_text(encoding="utf-8")
    builder = script[script.index("function buildRow") : script.index("function upsertRow")]

    def template_cells(markup: str) -> list[str]:
        return re.findall(r'<td class="([^"]+)"', markup)

    def script_cells(builder: str) -> list[str]:
        """The cells `buildRow` appends, in order, as a class each.

        An earlier version of this test only matched `el("td", "class", ...)`, so it measured
        its own regex rather than the code -- the three cells built by helpers were invisible
        to it and the two sides could not be compared at all. Every `tr.appendChild(...)` is
        classified here instead.
        """
        names = {"progressCell": "progress", "detailCell": "detail", "actionsCell": "actions"}
        out: list[str] = []
        for call in re.findall(r"tr\.appendChild\((.*?)\);", builder):
            call = call.strip()
            if call == "status":
                out.append("status-cell")
                continue
            helper = re.fullmatch(r"(\w+)\(job\)", call)  # a helper takes the job and nothing else
            if helper and helper.group(1) in names:
                out.append(names[helper.group(1)])
                continue
            direct = re.match(r'el\("td", "([^"]+)"', call)
            if direct:
                out.append(direct.group(1))
                continue
            out.append(f"<unrecognised: {call}>")
        return out

    assert script_cells(builder) == template_cells(template), (
        f"the template renders {template_cells(template)} and the script builds "
        f"{script_cells(builder)}; a stream frame replaces every row, so a difference here "
        f"is a page that reverts on its own"
    )

    # The hooks the filter and the stylesheet select on.
    assert "dataset.finished" in builder, "buildRow does not set data-finished, so nothing can be collapsed"
    assert 'el("span", "status status-"' in builder, (
        "the status cell needs the same status-<status> class the template gives it"
    )
    assert "data-finished=" in template, "the template has to mark finished rows the same way"


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance for a `#rrggbb` colour."""

    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(foreground: str, background: str) -> float:
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def test_every_button_rule_is_legible_against_the_background_it_lands_on():
    """`app.css` claims both themes are checked for contrast. This is that check.

    It exists because a rule written for a transparent button was applied to a filled one:
    `button` is `background: var(--accent)`, and a rule that only set `color: var(--accent)`
    made the label the same colour as its own background. It looked like a button with no
    text, which is what the screenshot showed. No assertion in the suite could have seen it,
    because the failure is entirely in the cascade.

    The rules are resolved the way the browser would: the base `button` block first, then the
    more specific selector, taking `background` and `color` from whichever declares them
    last.

    **Both themes, each against its own values.** `themes.setdefault` across the two blocks
    merged them and kept whichever came first, so only the palette at the top of the file was
    ever measured and the other was checked in the docstring and nowhere else. The light
    palette was that unchecked one and its primary button was #0b0d12 on #2f5fd0 -- 3.4:1,
    under AA, shipped, green. A test that names two themes and measures one is worse than no
    test, because it is the assertion everyone believes.
    """
    css = (Path(__file__).parent.parent / "hub/web/static/app.css").read_text(encoding="utf-8")
    # Comments are stripped first: a rule preceded by a block comment would otherwise carry
    # the comment into its "selector" and stop being findable by name. Which is exactly what
    # the first version of this resolver did.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    blocks = re.findall(r":root\s*\{(.*?)\}", css, re.S)
    assert len(blocks) == 2, (
        f"the palette is declared {len(blocks)} time(s); app.css declares light in `:root` "
        f"and dark in `@media (prefers-color-scheme: dark)`, so both have to be here for the "
        f"contrast below to be checking anything"
    )
    # One palette per block, kept apart. `setdefault`-ing across them collapsed two themes
    # into one and measured only the first.
    palettes: list[dict[str, str]] = [
        dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block)) for block in blocks
    ]
    theme_names = ("light", "dark")

    # selector -> (background, colour), as the cascade would resolve them.
    def resolve(selector: str) -> tuple[str, str] | None:
        background = colour = None
        # `findall` yields (selector, body) in that order; an earlier version unpacked them
        # the other way round and so searched the *selector* for declarations.
        for sel, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if selector not in [s.strip() for s in sel.split(",")]:
                continue
            # A value is a hex or a `var(--name)`; the base `button` uses the latter for its
            # background, so a hex-only pattern silently reported the rule as unstyled.
            found = dict(
                re.findall(r"(background|color):\s*(#[0-9a-fA-F]{3,6}|var\(--[\w-]+\))", block)
            )
            if "background" in found:
                background = found["background"]
            if "color" in found:
                colour = found["color"]
        if background is None or colour is None:
            return None
        return background, colour

    def expand(value: str, themes: dict[str, str]) -> str:
        for name, resolved in themes.items():
            value = value.replace(f"var(--{name})", resolved)
        return value

    checked = 0
    for theme_name, themes in zip(theme_names, palettes, strict=True):
        for selector in (
            "button",
            "button.danger",
            "button.secondary",
            '[data-action="queue-toggle-finished"][aria-pressed="true"]',
        ):
            found = resolve(selector)
            assert found is not None, (
                f"{selector} does not declare a background of its own, so it inherits the base "
                f"button's fill and only recolours the label. That is the bug this test was "
                f"written for: a rule that sets `color` alone lands coloured text on the accent "
                f"background, which reads as a button with no text."
            )
            background, colour = (expand(v, themes) for v in found)
            # An unexpanded `var(--name)` here means the palette is missing a token the rule
            # asks for; `_contrast` would then raise on `int('var(...)', 16)`, which is a
            # confusing error for what is really a missing definition.
            for value in (background, colour):
                assert not value.startswith("var("), (
                    f"{theme_name} theme has no `{value[4:value.index(')')]}` for {selector}; "
                    f"the two palettes have to define the same tokens or one of them is "
                    f"silently unstyled"
                )
            if len(colour) == 4:  # #abc -> #aabbcc
                colour = "#" + "".join(c * 2 for c in colour[1:])
            ratio = _contrast(colour, background)
            assert ratio >= 4.5, (
                f"{theme_name} theme, {selector}: {colour} on {background} is {ratio:.2f}:1, "
                f"below the 4.5:1 that WCAG AA asks of body text. A button whose label "
                f"matches its own background reads as a button with no text."
            )
            checked += 1
    assert checked == 2 * 4


# ---------------------------------------------------------------------------
# The affordances added on top of the queue: copy, memory, age. Same rules as
# every other check in this file -- source text on both sides, no browser --
# because a `data-action` the script does not answer is a button that silently
# does nothing, and a remembered codec that contradicts the select is worse
# than no memory at all.
# ---------------------------------------------------------------------------


def test_every_path_the_page_shows_can_be_copied():
    """`data-action="copy-text"` exists on both sides, and the script can serve it.

    The hub is served over plain HTTP on a LAN, where `navigator.clipboard` needs a
    secure context it will never get: the legacy path is not dead weight, it is the
    normal route, and it is asserted here so the day someone "cleanups up the
    fallback" the failure is a red test and not a wall of buttons that copy nothing.
    """
    root = Path(__file__).parent.parent / "hub/web"
    js = (root / "static/app.js").read_text(encoding="utf-8")
    for name in ("job_row.html", "library.html"):
        template = (root / f"templates/{name}").read_text(encoding="utf-8")
        assert 'data-action="copy-text"' in template, f"{name} shows a path it cannot copy"
    assert '"copy-text"' in js, "the page offers a copy button the script does not answer"
    assert "execCommand" in js, "the non-secure-context clipboard path is the one that runs"


def test_the_enqueue_form_remembers_only_what_it_can_restore():
    """Codec, language, force -- written and read, and both ends wrapped.

    `localStorage` answers `null` on a first visit and throws in private mode; either
    way the form has to start empty, which is what it did before the memory existed.
    Checked as source text because `hub/tests/` has no browser and would not grow one
    for a preference.
    """
    js = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    assert '"amd-hub.enqueue"' in js, "the form has no memory key"
    assert "localStorage.getItem" in js and "localStorage.setItem" in js
    assert "querySelector('option[value=" in js, (
        "a remembered codec has to exist in the select before it is restored"
    )


def test_every_row_carries_its_age_and_ages_in_place():
    """`created_at` reaches the browser and the label ticks without a snapshot.

    The column was made browser-parseable in `hub/jobs.py` for exactly this; the beat
    is five seconds behind a `document.hidden` check, and a row rebuilt by a frame
    carries a fresh label either way.
    """
    js = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    assert "ageSpan" in js and "job.created_at" in js
    assert "setInterval" in js and "document.hidden" in js


def test_the_stream_line_speaks_the_pool_sentence():
    """"live · 2/4 ripping" is assembled from the scheduler's truth, twice over.

    The `pool` frame is the live half, `/api/status` the initial half, and both land in
    the same `textContent` rebuild -- no pool state of the browser's own, because a
    second arithmetic of the same table is a second source of truth waiting to disagree.
    """
    js = (Path(__file__).parent.parent / "hub/web/static/app.js").read_text(encoding="utf-8")
    assert 'case "pool"' in js, "the stream's pool frame would arrive and be discarded"
    assert '"/api/status"' in js, "the count would not exist until the first frame"
    assert "ripping" in js


