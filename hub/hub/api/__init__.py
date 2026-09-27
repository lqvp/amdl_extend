"""The HTTP surface: one place that decides what a request is allowed to see.

Three things live here, and none of them belongs to a particular resource:

1. **The session guard.** Every `/api` router is created with `require_session` attached, so
   adding a route cannot forget it. The one exception in spec §9 is `GET /api/health`, which
   is installed separately precisely because it is the exception -- and it is installed here,
   next to the guard, so the pair reads together.

2. **`install(app)`**, the single place routers are attached. `create_app` calls it; the
   submodules are imported *inside* it rather than at the top of this file, because they all
   import the helpers defined below from here. That ordering is a real constraint, so it is
   expressed as a function-local import with the reason attached, rather than left to be
   rediscovered by the next reader who "tidies" it to the top.

3. **The HTML pages**, which are not the API. `POST /api/jobs` answers JSON; a browser that
   navigates to `/queue` must get a page or a redirect, never `{"detail": ...}` rendered as
   a website. The two differ only in what they do with an absent session -- 401 versus 303
   to the login form -- and they share `_is_authenticated` so they cannot disagree about who
   is logged in.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Imported under a qualified name, and the reason is a real trap: a bare
# `from hub import auth` binds the *name* `auth` on this package to `hub.auth`, and a later
# `from hub.api import auth` -- the submodule `api/auth.py` -- then finds that attribute
# already set and hands back `hub.auth` instead of importing the submodule. The failure reads
# as `AttributeError: module 'hub.auth' has no attribute 'login_router'`, which points at the
# wrong file entirely.
from hub import auth as hub_auth

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

#: The one cookie. **Imported, not restated.** It used to be a second literal here, and a
#: rename in either place would have left every session in the other half of the app
#: unverifiable -- `hub.auth` issues under its own name and this reads the name off the
#: request, so the two disagreeing is a hub where nothing logs in and nothing says why.
#: `test_the_cookie_name_has_exactly_one_definition` holds it.
SESSION_COOKIE = hub_auth.COOKIE_NAME

#: The page every unauthenticated navigation is sent to.
LOGIN_PATH = "/login"

#: Paths under `/static` that the *login page* must be able to load without a session.
#:
#: A stylesheet and a script that only style a page nobody can reach are a useless gate: the
#: login page would arrive unstyled and inert, and a user who mistypes their password would
#: be told so by a browser's default form rendering rather than the hub's. Everything else
#: under `/static` is behind the session, because "only `/api/health` is open" is a claim
#: that has to be true of the whole route table and not just of `/api` (M1, C2, C3).
PUBLIC_STATIC: frozenset[str] = frozenset({"/static/app.css", "/static/app.js"})


def build_templates() -> Jinja2Templates:
    """A Jinja2 environment over the package's own template directory.

    Built once per app and held on `app.state`, because `Jinja2Templates` owns a Jinja
    `Environment` and Jinja caches compiled templates inside it: a fresh environment per
    request would re-read and re-compile every template on every page load. Autoescaping is
    left on (Jinja2's default for `.html`) and nothing turns it off -- the queue renders
    paths read off the filesystem, and `test_a_scan_result_cannot_inject_markup_into_the_page`
    is what would notice.
    """
    return Jinja2Templates(directory=str(TEMPLATES_DIR))


def templates(request: Request) -> Jinja2Templates:
    """The app's environment.

    A fallback for a request whose `app` has none, so that a page rendered against a bare
    `FastAPI()` in a test or a REPL still works instead of raising `AttributeError` on
    `state`.
    """
    return getattr(request.app.state, "templates", None) or build_templates()


def fail(status: int, detail: str, **extra) -> JSONResponse:
    """A JSON error body: `detail` plus whatever else the caller can justify adding.

    `extra` exists for the two places where a machine-readable key is the whole point --
    `problem`, which keeps "no account is logged in" and "did not become ready" apart, and
    `retry_after`, which is what tells a rate-limited client when to come back. Everything
    else in the response is a string a person reads, and those strings are the
    collaborators' own messages, never rewritten.
    """
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def client_ip(request: Request) -> str:
    """The address the rate limit is keyed on: the ASGI **peer**, and nothing else.

    `request.client.host` is what the socket says, and it is the only value the client cannot
    forge. `X-Forwarded-For` is a request *header* -- a string the caller chose -- so reading
    it would hand the limiter its own key: `X-Forwarded-For: <random>` per request is ten
    free guesses against a 10-per-5-minutes budget, and the limiter stops bounding anything
    at all. It is also the single easiest "improvement" anyone could make to this function
    (the report named the shared-bucket cost behind a proxy as the motivation), which is why
    `test_the_rate_limit_key_is_the_peer_and_not_a_forwarded_header` asserts the *key* rather
    than a count -- swapping the read leaves the whole suite green otherwise.

    The cost is real and named: behind a TLS-terminating reverse proxy every request shares
    the proxy's bucket, so one attacker would lock out every user. The honest fix for that is
    a proxy that does not let arbitrary clients set the header, or a hub that binds a port
    only its proxy can reach -- both of which are topology decisions the hub has no setting
    for. Refusing to read the header is the option that cannot be silently wrong.
    """
    client = request.client
    return client.host if client is not None and client.host else "unknown"


def session_generation(request: Request) -> int:
    """The generation this request is judged against: the app's, or 0 with no app state.

    Read from `app.state` on every check rather than captured once, because bumping it is
    what revokes every outstanding session at once. **Logout is the only thing that bumps it
    today.** This sentence used to add "a password change, a compromise response", and
    `hub.auth.SessionStore.issue` carries the careful version of the same correction; neither
    was true, and a reader adding a change-password route would more likely find this one
    first. The password is a single value read from the environment at start-up, so there is
    no endpoint to add one to: a changed password takes effect at the next restart, and the
    image is where it is rotated. If such a route is ever added, the line is
    `app.state.session_generation = app.state.sessions.retire(current)`, and the contract to
    test is "the old token is refused afterwards".

    Read at call time is the whole mechanism; a captured value would make the bump do nothing.
    """
    return int(getattr(request.app.state, "session_generation", 0))


def is_authenticated(request: Request) -> bool:
    """Whether this request carries a session this process issued, and still honours.

    "Still honours" is the `generation` argument, and it is what makes `POST
    /api/auth/logout` a revocation rather than a request to the browser. The handler clears
    the cookie; the generation bump makes the token worthless to anybody who kept a copy.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return bool(
        request.app.state.sessions.verify(token, generation=session_generation(request))
    )


async def require_session(request: Request) -> None:
    """The guard. `None` on success, so it composes as a plain dependency.

    401 and not 403: there is no session, so the client is not forbidden from a resource it
    was never allowed -- it has not identified itself. The `WWW-Authenticate` header is
    absent on purpose; this is a cookie scheme and naming a scheme the server does not speak
    would be a lie a client might act on.
    """
    if is_authenticated(request):
        return
    raise _Unauthorized()


class _Unauthorized(Exception):
    """Internal: turned into a 401 by the handler registered in `install`.

    An exception rather than an `HTTPException` so that raising it from a *page* route is a
    bug rather than a 401 in the middle of rendering: pages redirect, and they do it
    explicitly.
    """


def guarded() -> APIRouter:
    """A router on which **every** route requires a session.

    One factory rather than `dependencies=[Depends(require_session)]` copied into four
    modules, because the copy is the failure mode this exists to prevent: a route added to a
    router that is missing the argument is reachable by anyone on the LAN, and nothing about
    it looks wrong. `test_every_route_but_health_requires_a_session` is the assertion
    surface; this is what makes the omission visible in review as well.
    """
    return APIRouter(dependencies=[Depends(require_session)])


class GuardedStatic(StaticFiles):
    """`StaticFiles` behind the session guard, with the login page's own two files open.

    A `Mount` is not an `APIRoute`, so `dependencies=` on a router does not reach it -- the
    mount was outside `guarded()` and served `/static/app.css` to anybody. It cannot be
    brought inside a router, so the check is here instead, at the one place that answers for
    it.

    `PUBLIC_STATIC` is the whole exception list, and it is two files: the login page has to be
    able to render without a session, and a login form with no stylesheet and no script is a
    worse answer than a styled one. Nothing else under `/static` is readable without a
    session -- and `test_the_static_mount_is_behind_the_session_guard` walks the directory and
    checks every file against the list, so a new asset added tomorrow is a 401 by default and
    only becomes public by a deliberate edit here.
    """

    def __init__(
        self, *args, public: frozenset[str] = PUBLIC_STATIC, prefix: str = "/static", **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._public = public
        self._prefix = prefix

    async def get_response(self, path: str, scope) -> StreamingResponse:
        # `StaticFiles.get_path` hands over the path *relative to the mount*, with the mount
        # prefix already stripped: a request for `/static/app.css` arrives here as
        # `"app.css"`. The mount prefix is put back so the key is the URL a browser asked
        # for, which is the string `PUBLIC_STATIC` is written in and the string
        # `test_the_static_mount_is_behind_the_session_guard` compares against.
        served = f"{self._prefix}/{path.lstrip('/')}"
        if served not in self._public and not is_authenticated(_Request(scope)):
            # `_Unauthorized`, which `install`'s handler turns into a 401 -- the same answer
            # as every guarded route, from the same place, with no second message.
            raise _Unauthorized()
        return await super().get_response(path, scope)


def _Request(scope) -> Request:  # noqa: N802 - a scope is the input, not a request
    """A `Request` built from an ASGI scope, for a mount that only has a scope.

    `StaticFiles.get_response` is handed a `scope` and not a `Request`, and building one is
    the only way to ask the shared `is_authenticated` question rather than re-implementing
    the cookie read in a second place.
    """
    return Request(scope, receive=_empty_receive)


async def _empty_receive() -> dict:
    """A receive that never yields, because a static GET has no body to read."""
    return {"type": "http.disconnect"}


async def page_session(request: Request) -> bool:
    """For a page route: `True` when there is a session, `False` when the caller should
    redirect. Never raises, because a redirect is the correct answer for a browser."""
    return is_authenticated(request)


def _redirect_to_login() -> RedirectResponse:
    """303, not 307: a login page reached by POST must be re-fetched with GET.

    `See Other` is also what keeps the submitted password out of the address bar and out of
    the history entry, which a 302-with-POST preserved would not.
    """
    return RedirectResponse(LOGIN_PATH, status_code=303)


#: Sent with every response (M5). Defence in depth, and one of the three is a *correctness*
#: requirement rather than a nicety:
#:
#: - `X-Content-Type-Options: nosniff` stops a browser from re-interpreting a response as a
#:   type the server did not send. The hub serves JSON and HTML and nothing else, and
#:   `nosniff` means a JSON body cannot be turned into script by a `text/plain`-style guess.
#: - `X-Frame-Options: DENY` stops any page embedding the queue in an iframe. There is no
#:   legitimate framing of a single-user tool, and a framed page can be used to make a
#:   clickjacking target out of a form that deletes files.
#: - `Content-Security-Policy` with `default-src 'self'` is the one that would actually stop
#:   an injected script if an escaping bug appeared. **`'unsafe-inline'` is absent and must
#:   stay absent**: the templates put no `<script>` block in a page, so nothing needs it, and
#:   adding it back would remove the only protection against a template that starts trusting a
#:   filesystem-derived string.
#:
#: `frame-ancestors 'none'` is the modern spelling of the second and is what a browser uses
#: when it is understood; `X-Frame-Options` stays for the ones that do not. Neither costs
#: anything here because nothing frames this.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    # Referrers would otherwise carry a hub URL to whatever the page links to. The queue links
    # to `music.apple.com` in a "source" link, and `/queue` itself is not secret -- but a
    # `Referer` is free to not send.
    "Referrer-Policy": "no-referrer",
}


def install(app: FastAPI) -> None:
    """Attach every router, the static files and the error handlers to `app`."""
    from hub.api import auth as auth_routes
    from hub.api import jobs as job_routes
    from hub.api import library as library_routes
    from hub.api import wrapper as wrapper_routes

    @app.middleware("http")
    async def security_headers(request: Request, call):
        """Put `SECURITY_HEADERS` on every response, whatever produced it.

        A middleware rather than headers on each response class, because the responses that
        matter here are the ones nobody thinks to add headers to: the 401, the 303 from a page,
        the SSE stream, the static files and the 502 from a collaborator. A `StaticFile`
        response in particular is built inside Starlette and cannot be decorated from a route.

        **Including the ones an exception handler produced**, which was the doubt when this
        was written: `app.middleware("http")` is *outside* the `ExceptionMiddleware` in the
        stack, so a handler's `JSONResponse` is the response `call` returns and passes back
        through here like any other. There was a second helper for the case where it would
        not, and a docstring asserting it did; both were wrong, verified by asking the 401
        route directly, and are gone. `test_the_security_headers_are_on_the_responses_that_`
        matter keeps asking rather than trusting this paragraph.
        """
        response = await call(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(_Unauthorized)
    async def _unauthorized(_request: Request, _exc: _Unauthorized) -> JSONResponse:
        return fail(401, "authentication required: POST the shared password to "
                         f"{auth_routes.LOGIN_PATH} to get a session")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # One handler, so that a `RuntimeError` from a collaborator is reported as a message
        # rather than as a stack trace. `SupervisorError` and `RipperHostError` are both
        # `RuntimeError` subclasses *by design* (`ripper_host.py`'s own docstring says so),
        # and their messages are written to be shown -- so they are passed through, and the
        # type is named beside them.
        from hub.resolver import ResolveError
        from hub.ripper_host import RipperHostError
        from hub.wrapper_supervisor import SupervisorError

        if isinstance(exc, (ResolveError, SupervisorError, RipperHostError)):
            return fail(502, f"{type(exc).__name__}: {exc}")
        # Anything else is a bug, and a bug in a *handler* is logged by uvicorn anyway;
        # re-raising keeps the traceback that says where it came from.
        raise exc

    # The healthcheck's target, and the only route reachable without a session. Installed
    # before the guarded routers so its path is the one that matches first, and so the
    # exception to the guard is visible in the same place as the guard.
    app.include_router(health_router)
    # The login route is the one `/api` router that is *not* guarded -- a route that must be
    # reachable without a session cannot be a member of a router whose every member is. It is
    # included here, next to the guard and next to health, so the two exceptions are read
    # together rather than found in two files.
    app.include_router(auth_routes.login_router)

    for router in (
        auth_routes.router,
        wrapper_routes.router,
        job_routes.router,
        library_routes.router,
    ):
        app.include_router(router)

    app.include_router(pages_router)
    # Guarded, because a `Mount` is not an `APIRoute` and so is outside every `guarded()`
    # router's reach. Only `PUBLIC_STATIC` is open, and the login page needs it (C2).
    app.mount(
        "/static",
        GuardedStatic(directory=str(STATIC_DIR)),
        name="static",
    )


# --------------------------------------------------------------------------- #
# The one unguarded route
# --------------------------------------------------------------------------- #
health_router = APIRouter()


@health_router.get("/api/health")
async def health() -> dict:
    """Liveness, and nothing else.

    This is the compose healthcheck's `curl` target, so it has to answer before anybody has
    logged in. That makes it the one route a stranger can reach, and therefore the one place
    where a useful-looking extra field would be a disclosure: the wrapper's state, the queue
    depth, the library roots and the versions all describe the host, and there is a
    `/api/status` for the authenticated caller who is allowed to see them.
    """
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# The pages
# --------------------------------------------------------------------------- #
pages_router = APIRouter()


def _page_context(request: Request, **extra) -> dict:
    """What every template is given, in one place.

    `request` itself is under the name templates expect, and `authenticated` is passed
    explicitly rather than being re-derived in each template -- a template that worked out
    its own auth from a cookie would be a second implementation of `is_authenticated`.
    """
    return {"request": request, "authenticated": is_authenticated(request), **extra}


@pages_router.get("/login")
async def login_page(request: Request, error: int = 0) -> object:
    """The login form. Reachable with or without a session, so a logged-in user can get back."""
    return templates(request).TemplateResponse(
        request,
        "login.html",
        _page_context(
            request,
            failed=bool(error),
            # The message comes from the one constant rather than being written into the
            # template. Two homes for one string is how the two drift, and the POST
            # handler's docstring already claimed the page rendered `auth.LOGIN_FAILED`.
            failed_message=hub_auth.LOGIN_FAILED,
            settings=request.app.state.settings,
        ),
    )


@pages_router.post("/login")
async def login_submit(request: Request) -> RedirectResponse:
    """The form's target, and a no-JS path to a session.

    A plain form post, so the hub is usable with scripting disabled: this is a LAN tool for
    one person, and a page that needs JavaScript to authenticate is a page that cannot
    authenticate when a proxy mangles one script. The JSON route is the same logic with a
    JSON answer, and both call `hub.api.auth._authenticate`.

    **A failure redirects rather than rendering a 401**, because a browser navigating to a
    401 gets a JSON body in the place of a page. The error is a query flag and not a message:
    the page renders `auth.LOGIN_FAILED` itself, so nothing about the attempt is carried in
    a URL that ends up in history and in the `Referer` of the next request.
    """
    from hub.api.auth import _authenticate, set_session_cookie

    form = await request.form()
    try:
        ok = await _authenticate(request, form.get("password"))
    except auth_rate_limited() as limited:
        return RedirectResponse(
            f"/login?error=1&retry={int(max(1, round(limited.retry_after)))}", status_code=303
        )
    if not ok:
        return RedirectResponse("/login?error=1", status_code=303)

    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, request)
    return response


@pages_router.post("/logout")
async def logout_page(request: Request) -> RedirectResponse:
    """The page's logout. Retires the session, and redirects to the login form.

    **Unauthenticated on purpose, and it has to be** (M1). A cross-site logout -- a page
    anywhere posting a form at `<hub>/logout` -- is a nuisance, not a breach: it revokes a
    session the user already had and sends them to a login form. Guarding it would mean a
    cross-site form could not log you out but *could* have done nothing else useful, and the
    cost is that a browser following a redirect with a stale cookie would get a 401 as a
    login form instead of a redirect. The important half is that this retires the generation,
    not that it clears a cookie, and that is exactly what the JSON route does.

    It is on the `OPEN_WITHOUT_A_SESSION` list in `tests/test_api_jobs.py` so the route-table
    test accounts for it rather than being surprised by it.
    """
    from hub.api.auth import _clear_session

    request.app.state.session_generation = request.app.state.sessions.retire(
        session_generation(request)
    )
    return _clear_session(request, RedirectResponse(LOGIN_PATH, status_code=303))


def auth_rate_limited():
    """`hub.auth.RateLimited`, so the two login handlers catch the *same* class.

    Catching one and letting the other propagate would be a 500 on the form path, which is
    the one a user is looking at. Indirection through a function so the two routes cannot
    drift onto two different classes.
    """
    return hub_auth.RateLimited


@pages_router.get("/")
async def index(request: Request) -> object:
    return await _queue_page(request)


@pages_router.get("/queue")
async def queue(request: Request) -> object:
    return await _queue_page(request)


async def _queue_page(request: Request):
    """The queue, with the wrapper's state and the library's reachability above it.

    One page rather than two, because the two things a user needs at the moment they press
    "download" are the same two things: is anything running, and is the drive I am about to
    write to mounted.
    """
    from hub.api.jobs import job_to_dict
    from hub.api.wrapper import wrapper_state

    if not await page_session(request):
        return _redirect_to_login()
    state = request.app.state
    return templates(request).TemplateResponse(
        request,
        "queue.html",
        _page_context(
            request,
            settings=state.settings,
            wrapper=await wrapper_state(state),
            library=await _library_summary(state),
            jobs=[job_to_dict(job) for job in state.jobs.list()],
            queue_counts=state.jobs_counts(),
        ),
    )


@pages_router.get("/library")
async def library_page(request: Request) -> object:
    from hub.api.library import library_listing

    if not await page_session(request):
        return _redirect_to_login()
    state = request.app.state
    return templates(request).TemplateResponse(
        request,
        "library.html",
        _page_context(
            request, settings=state.settings, listing=await library_listing(state)
        ),
    )


async def _library_summary(state) -> dict:
    """Roots, which of them could be read, and how many album directories each one has.

    From a real `scan_roots` and nothing cached, because spec §7.1 measures the walk at
    0.06 s and a stale "1 album found" on an unmounted drive is worse than a slow page.

    `per_root` is positional with `roots`, and it is here rather than only on the library page
    because `/api/status` is what an operator or a monitor reads: a drive that is not plugged
    in can be mounted-and-empty rather than missing, and then `degraded_roots` is empty and
    nothing says so. `albums` alone cannot distinguish that from a full library; the
    per-root count can.
    """
    import asyncio

    from hub.library_scan import scan_roots

    scan = await asyncio.to_thread(scan_roots, state.settings.library_roots)
    return {
        "roots": [str(root) for root in scan.roots],
        "degraded_roots": [str(root) for root in scan.degraded],
        "per_root": list(scan.per_root()),
        "albums": len(scan.albums),
    }
