"""Login, logout, and the session cookie (spec §9, §11).

One shared password, one signed cookie, one message for every failure. The JSON routes and
the form routes are the same function with a different answer, because a login that works
with JavaScript and a different one that works without it would be two implementations of a
security boundary.

**The failure message is a constant and it is the only one.** `hub.auth.LOGIN_FAILED` is
returned for a wrong password, an empty field, a field that is not a string and a body that
is not a form at all. There is one secret, so there is nothing to distinguish; and echoing
the submitted value would turn the form into a reflection point. The rate limit's answer is
the one *different* response, and it is different on purpose -- §11 requires the limit, and a
client that is not told when to come back will come back immediately and make the limit
useless.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from hub import auth
from hub.api import fail, guarded, is_authenticated, require_session, session_generation

# The one message. Re-exported under this name so a reader of this file does not have to
# open `auth.py` to learn that the other answers are not coming from here.
LOGIN_PATH = "/api/auth/login"
LOGIN_FAILED = auth.LOGIN_FAILED

router = guarded()
# The login route is unguarded, and has to be: a route that must be reachable without a
# session cannot be a member of a router whose every member requires one. Two routers rather
# than one with a hole punched in it, so the hole is named.
login_router = APIRouter()
# Imported for its side effect on the module's public surface -- `require_session` is the
# guard `guarded()` installs, and keeping the name importable here is what lets a reader of
# this file see that the dependency is the same one and not a second implementation.
__all__ = ["LOGIN_FAILED", "LOGIN_PATH", "login_router", "require_session", "router"]


async def _submitted_password(request: Request) -> object:
    """Whatever the caller sent as the password, or `None` if they sent nothing usable.

    Both shapes, because §9 describes the API as JSON and the login *page* is a form, and
    `python-multipart` is a declared dependency for exactly that reason. Dispatching on the
    content type rather than trying both means a JSON body is never run through the form
    parser, which would otherwise raise on a body that is not one.

    Not a `str` in, `str` out: the value goes to `auth.verify_password`, which refuses a
    non-string, so `{"password": 5}` produces the same 401 as a wrong password instead of a
    500 on the one route a stranger can reach.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - a body that is not JSON is a wrong password
            return None
        return payload.get("password") if isinstance(payload, dict) else None
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - see above
        return None
    return form.get("password")


async def _authenticate(request: Request, password: object) -> bool:
    """The whole login decision, for both routes. `True` on success.

    Rate limit first, then the comparison, then the budget back. The order is the design:
    the limit is the only thing bounding guesses, because the comparison is constant-time
    and therefore no slower for a near-miss than for a wild one.
    """
    store = request.app.state.sessions
    ip = _ip(request)
    store.check_rate_limit(ip)
    if not auth.verify_password(password, request.app.state.settings.password):
        return False
    store.forget(ip)
    return True


def _ip(request: Request) -> str:
    from hub.api import client_ip

    return client_ip(request)


def set_session_cookie(response: Response, request: Request) -> None:
    """Put the session on `response`, with the attributes spec §11 lists.

    `Secure` follows the request rather than a setting, because a hardcoded one breaks the
    plain-HTTP LAN deployment and its absence hands the session to the local network. Behind a
    TLS-terminating proxy the scheme is `http` and the attribute is therefore not set --
    `auth.is_tls_request` documents that terminate at uvicorn.

    The token carries the app's **current** generation, so it is live now and dead the moment
    anyone bumps the counter.
    """
    from hub.api import SESSION_COOKIE, session_generation

    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.issue(generation=session_generation(request)),
        max_age=auth.DEFAULT_MAX_AGE,
        httponly=auth.COOKIE_HTTPONLY,
        samesite=auth.COOKIE_SAMESITE,
        secure=auth.is_tls_request(request),
        path="/",
    )


def _clear_session(request: Request, response: Response) -> Response:
    """Drop the cookie.

    The attributes must *match* the ones it was set with or the browser keeps the original:
    a `Set-Cookie` for the same name that differs only in `Path` is a different cookie, and
    the old one is still on the request the next time.

    **This is tidiness, not revocation.** The token in the copy the browser is being told to
    forget stays valid until the generation moves, and the generation moves in the two
    logout handlers rather than here -- so that both the JSON route and the page route
    retire it exactly once, in the place that owns the decision.
    """
    from hub.api import SESSION_COOKIE

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=auth.COOKIE_HTTPONLY,
        samesite=auth.COOKIE_SAMESITE,
        secure=auth.is_tls_request(request),
    )
    return response


@login_router.post("/api/auth/login")
async def login(request: Request) -> Response:
    password = await _submitted_password(request)
    try:
        ok = await _authenticate(request, password)
    except auth.RateLimited as limited:
        response = fail(
            429,
            f"too many login attempts from this address; try again in "
            f"{limited.retry_after:.0f}s",
            retry_after=round(limited.retry_after),
        )
        response.headers["Retry-After"] = str(max(1, round(limited.retry_after)))
        return response

    if not ok:
        return fail(401, LOGIN_FAILED)
    response = JSONResponse({"authenticated": True})
    set_session_cookie(response, request)
    return response


@router.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    """Retire every outstanding session, then drop this browser's cookie.

    **The generation bump is the point; the cookie is tidiness.** A bearer cookie cannot be
    withdrawn by asking the holder to forget it -- anybody who copied the token keeps a
    working session for the rest of its `max_age` -- so logout increments
    `app.state.session_generation` and every token ever issued stops verifying at once.

    That revokes *every* session rather than this one, which is the honest trade for a
    single-password tool with no session table: there is nothing per-session to delete, and
    the alternative is a table that has to be persisted, backed up and expired. A user with
    one browser is unaffected; a user who logged in on a laptop and a phone is asked for the
    password once more.
    """
    request.app.state.session_generation = request.app.state.sessions.retire(
        session_generation(request)
    )
    return _clear_session(request, JSONResponse({"authenticated": False}))


@router.get("/api/auth/session")
async def session(request: Request) -> dict:
    """Whether there is a session. Reached only *with* one, so the answer is `True`.

    It is behind the guard because §9 lists it among the session-authenticated routes, and
    "is anybody logged in" from an unauthenticated caller is a question worth not answering
    on a shared LAN. The login page and the redirect logic use `is_authenticated` directly.
    """
    return {"authenticated": is_authenticated(request)}
