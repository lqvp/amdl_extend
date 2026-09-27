"""Authentication: one password, one signed cookie, one rate limit.

`auth.py` is the only thing standing between the LAN and every authenticated endpoint, so
these tests are about the *properties* it is required to have rather than about a code
path: a constant-time comparison, a bounded number of attempts per address, and a cookie
that cannot be forged or read from script.

Two of them came first, verbatim, because they are the two that would be silently
"satisfied" by a wrong implementation: `==` passes `test_password_compared_in_constant_time`
if the test only checked the answer, and a limiter that counts per *process* instead of per
*IP* passes `test_rate_limit_blocks_after_ten_attempts` for a single address.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import hub.auth as auth

SECRET = b"x" * 32


# --------------------------------------------------------------------------- #
# The password comparison
# --------------------------------------------------------------------------- #
def test_password_compared_in_constant_time(monkeypatch):
    # must not use ==; assert compare_digest is actually called
    called: list[tuple] = []
    monkeypatch.setattr(
        auth.secrets, "compare_digest", lambda a, b: called.append((a, b)) or True
    )
    auth.verify_password("pw", "pw")
    assert called == [("pw", "pw")]


def test_verify_password_returns_the_comparison(monkeypatch):
    """The patched `compare_digest` above returns True whatever it is given.

    If `verify_password` did not return its result, that test would still pass while the
    function answered `None` -- i.e. every login would fail and the suite would be green.
    """
    monkeypatch.setattr(auth.secrets, "compare_digest", lambda a, b: True)
    assert auth.verify_password("anything", "expected") is True
    monkeypatch.setattr(auth.secrets, "compare_digest", lambda a, b: False)
    assert auth.verify_password("expected", "anything") is False


def test_a_non_string_candidate_is_refused_without_raising():
    """`compare_digest` raises `TypeError` on a str/bytes mismatch.

    FastAPI hands a form field a `str` and a JSON body whatever the client sent, so a
    `{"password": 5}` would otherwise be a 500 on the login page -- an unhandled crash on
    the one route a stranger can reach, in exchange for nothing.
    """
    assert auth.verify_password(None, "pw") is False
    assert auth.verify_password(5, "pw") is False
    assert auth.verify_password(b"pw", "pw") is False


def test_a_wrong_password_is_refused():
    assert auth.verify_password("pw", "other") is False


# --------------------------------------------------------------------------- #
# The rate limit
# --------------------------------------------------------------------------- #
def test_rate_limit_blocks_after_ten_attempts():
    store = auth.SessionStore(secret=SECRET, max_attempts=10, window=300.0)
    for _ in range(10):
        store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")


def test_the_limit_is_per_address():
    """Ten failures from one machine must not lock out the rest of the LAN.

    The default bind is `0.0.0.0`, so the hub is shared: a laptop that fat-fingers
    its password ten times would otherwise lock out the phone on the same wifi for five
    minutes. A single global counter is the shape this test exists to forbid.
    """
    store = auth.SessionStore(secret=SECRET, max_attempts=10, window=300.0)
    for _ in range(10):
        store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")
    # A different address has its own budget, so ten calls on it are also fine...
    for _ in range(10):
        store.check_rate_limit("10.0.0.2")
    # ...and its eleventh is refused for the same reason, which is what says the budget
    # moved rather than being deleted along with the first address.
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.2")


def test_the_window_slides_rather_than_latching():
    """The limit is a sliding window, not a permanent ban.

    A latching counter needs an out-of-band reset, and the only reset a browser has is
    restarting the hub. Five minutes after the last mistake the user should be able to try
    again -- that is the interval the limit is defined over.
    """
    now = [1_000.0]
    store = auth.SessionStore(secret=SECRET, max_attempts=3, window=300.0, clock=lambda: now[0])
    for _ in range(3):
        store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")

    now[0] += 301.0
    # The window has moved past all three attempts, so this address is as fresh as a new one:
    # three more calls land, and the fourth is refused again rather than being let through
    # because the old stamp was still in the deque.
    for _ in range(3):
        store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")


def test_a_successful_login_clears_the_counter():
    """Ten *correct* logins must not lock the user out of their own hub.

    `check_rate_limit` records the attempt before the password is looked at, which is what
    makes the rate limit useless against an attacker who cannot be timed -- so a real
    success has to give the budget back, or a user who re-logs-in after every restart is
    locked out by their own correct password.
    """
    store = auth.SessionStore(secret=SECRET, max_attempts=2, window=300.0)
    store.check_rate_limit("10.0.0.1")
    store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")
    store.forget("10.0.0.1")
    store.check_rate_limit("10.0.0.1")


def test_retry_after_is_bounded_by_the_window_and_counts_down():
    now = [1_000.0]
    store = auth.SessionStore(secret=SECRET, max_attempts=2, window=300.0, clock=lambda: now[0])
    store.check_rate_limit("10.0.0.1")
    store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited) as raised:
        store.check_rate_limit("10.0.0.1")
    # The first attempt is the one that ages out, so the answer is bounded by the window and
    # is not zero -- a `Retry-After: 0` tells a client to hammer the route it just locked.
    assert 0 < raised.value.retry_after <= 300.0
    now[0] += 100.0
    with pytest.raises(auth.RateLimited) as later:
        store.check_rate_limit("10.0.0.1")
    assert later.value.retry_after < raised.value.retry_after


def test_retry_after_is_zero_for_an_address_that_is_not_limited():
    store = auth.SessionStore(secret=SECRET, max_attempts=1, window=300.0)
    assert store.retry_after("10.0.0.9") == 0.0


def test_expired_addresses_are_evicted():
    """The limiter's own bookkeeping must not grow without bound.

    Every rejected connection is a key, and the hub binds `0.0.0.0`, so a scan is a cheap way
    to grow a dict that nothing else ever prunes. `_MAX_TRACKED_IPS` is a backstop and this is
    what says it is reached; without it the cap is a comment.
    """
    now = [1_000.0]
    store = auth.SessionStore(secret=SECRET, max_attempts=1, window=1.0, clock=lambda: now[0])
    for index in range(auth._MAX_TRACKED_IPS * 2):
        store.check_rate_limit(f"10.0.{index // 256}.{index % 256}")
    # The cap is enforced by a sweep, and the sweep fires on a threshold rather than every
    # call -- so the bound that is actually held is the threshold, and it is the one the
    # eviction is measured against. `_MAX_TRACKED_IPS` remains the target the sweep *aims*
    # for, and `test_a_sweep_actually_removed_something` is what says the aiming works.
    assert store.tracked_ips() <= auth._PRUNE_THRESHOLD

    now[0] += 10.0  # every window has moved past, so the next sweep frees all of them
    store.check_rate_limit("192.168.9.9")
    assert store.tracked_ips() <= auth._PRUNE_THRESHOLD


def test_the_limiter_sweeps_amortisedly_not_on_every_call(monkeypatch):
    """One sweep per threshold's worth of attempts, not one per attempt (M4).

    The old `_prune` walked the whole dict on every call, inside an `async` handler: a sweep
    from a thousand addresses cost a thousand O(n) passes on the event loop, which is the one
    thread the SSE stream, the scheduler and every other request need. So the sweep is counted
    here, by wrapping the private method -- which is a *count of sweeps*, so an implementation
    that swept more often than the threshold allows fails, and so does one that never sweeps
    (which `test_a_sweep_actually_removed_something` covers from the other side).
    """
    store = auth.SessionStore(secret=SECRET, max_attempts=100, window=10_000.0)
    attempts = auth._MAX_TRACKED_IPS * 2

    # What is counted is *walks of the whole table*, not calls to `_prune` -- the method is
    # still called every time, and that is fine; what must not happen is the O(n) walk. So the
    # limiter's own dict is swapped for one that counts `items()`, which is the comprehension
    # the sweep is built from. This is the cost being complained about, measured directly.
    class _CountingAttempts(dict):
        walks = 0

        def items(self):
            _CountingAttempts.walks += 1
            return super().items()

    store._attempts = _CountingAttempts()  # noqa: SLF001 - the cost under test
    for index in range(attempts):
        store.check_rate_limit(f"10.0.{index // 256}.{index % 256}")

    # One sweep costs *two* walks: the stale-entry comprehension, and `sorted` for the
    # eviction when the sweep did not free enough. So `allowed` is twice the number of
    # threshold-crossings, plus a little slack for the final one.
    allowed = 2 * (attempts // auth._PRUNE_THRESHOLD) + 2
    assert _CountingAttempts.walks <= allowed, (
        f"{_CountingAttempts.walks} full walks of the address table for {attempts} attempts "
        f"(allowed {allowed}); the table is being scanned far more often than the threshold "
        f"permits"
    )
    assert _CountingAttempts.walks >= 1, "nothing was ever swept, so the cap is a comment"


def test_a_sweep_actually_removed_something():
    """The amortised sweep is not a no-op.

    "It only sweeps sometimes" and "it never sweeps" are the same shape of bug from the
    outside, so the observable is a dict that shrank.
    """
    now = [1_000.0]
    store = auth.SessionStore(secret=SECRET, max_attempts=1, window=1.0, clock=lambda: now[0])
    # Just *under* the threshold, so the last attempt in the loop did not trigger a sweep and
    # the size is genuinely everything recorded. A window of 1 s against a frozen clock means
    # nothing is stale, so this is the dict at its largest.
    for index in range(auth._PRUNE_THRESHOLD):
        store.check_rate_limit(f"10.0.{index // 256}.{index % 256}")
    grown = store.tracked_ips()
    assert grown == auth._PRUNE_THRESHOLD, "the loop did not record what it should have"

    # Every window moves past, then one more address arrives and tips it over the threshold,
    # so the sweep runs and finds every entry stale.
    now[0] += 10.0
    store.check_rate_limit("192.168.9.9")
    assert store.tracked_ips() == 1, (
        f"the dict reached {grown} and one sweep left {store.tracked_ips()} entries; a sweep "
        f"with every window moved past should leave only the address just added"
    )


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #
def test_token_roundtrip():
    store = auth.SessionStore(secret=SECRET)
    assert store.verify(store.issue())
    assert not store.verify("forged")


def test_a_token_signed_with_another_secret_is_refused():
    mine = auth.SessionStore(secret=SECRET)
    theirs = auth.SessionStore(secret=b"y" * 32)
    assert not mine.verify(theirs.issue())


def test_a_token_past_its_max_age_is_refused():
    """An unbounded session would outlive the password it was issued under.

    `AMD_PASSWORD` can be changed while the hub runs, and nothing here re-reads it -- so the
    age bound is the only thing that makes a rotated password take effect for sessions that
    already exist.
    """
    store = auth.SessionStore(secret=SECRET, max_age=60)
    assert not store.verify(_signed_seconds_ago(SECRET, auth.SESSION_SALT, 3600))


def test_a_token_within_its_max_age_is_accepted():
    store = auth.SessionStore(secret=SECRET, max_age=3600)
    assert store.verify(_signed_seconds_ago(SECRET, auth.SESSION_SALT, 60))


def test_a_token_from_a_retired_generation_is_refused():
    """The revocation, at the level it is implemented.

    Without the comparison a logout is a suggestion to the browser: the cookie is cleared and
    anybody holding a copy of the token keeps a working session for the rest of its `max_age`.
    That is the whole threat model for a bearer cookie, and it is why `verify` takes a
    generation rather than only checking the signature.
    """
    store = auth.SessionStore(secret=SECRET)
    token = store.issue(generation=4)
    assert store.verify(token, generation=4)
    # The next logout.
    assert not store.verify(token, generation=5)
    # And an older generation cannot be replayed into a *lower* one either.
    assert not store.verify(token, generation=0)
    assert not store.verify(token, generation=3)


def test_generation_survives_in_the_signed_payload():
    """The generation is inside the signature, so it cannot be edited.

    A payload field the signature does not cover would be attacker-chosen: mint a token with
    the current generation and then rewrite the number. This unpacks the token and looks at
    the field; the round-trip assertion above is what says the signature covers it.
    """
    import itsdangerous

    token = auth.SessionStore(secret=SECRET).issue(generation=7)
    payload = itsdangerous.URLSafeTimedSerializer(SECRET, salt=auth.SESSION_SALT).loads(token)
    assert payload["g"] == 7
    # A token with no `g` at all is from before the field existed and is not a live session.
    legacy = itsdangerous.URLSafeTimedSerializer(SECRET, salt=auth.SESSION_SALT).dumps({"v": 1})
    assert not auth.SessionStore(secret=SECRET).verify(legacy, generation=0)


def test_retire_always_moves_on():
    """Two logouts racing must not land on the same generation.

    `retire` is a counter rather than a random value for exactly this: with a random
    generation, two concurrent logouts could pick the same one and a token issued between them
    would stay valid.
    """
    store = auth.SessionStore(secret=SECRET)
    assert store.retire(0) == 1
    assert store.retire(1) == 2
    assert store.retire(2) == 3


@pytest.mark.parametrize("token", ["", "forged", "a.b.c", None, 5, b"bytes"])
def test_anything_that_is_not_a_token_is_refused_without_raising(token):
    """`verify` runs on every request against a cookie the client chose.

    A `TypeError` out of `itsdangerous` on a non-string would be a 500 for anyone who edits
    their own cookie, and an unauthenticated 500 is a worse answer than a 401: it tells them
    the guard ran, and it is a traceback on a public route.
    """
    assert auth.SessionStore(secret=SECRET).verify(token) is False


def test_a_token_whose_payload_is_not_a_session_is_refused():
    """A valid signature over the wrong payload is still not a session.

    The secret is process-wide, so anything that can sign can also sign `{"v": 99}`. The
    version is what makes a future format change a deliberate decision instead of a
    silently-accepted old cookie.
    """
    import itsdangerous

    token = itsdangerous.URLSafeTimedSerializer(SECRET, salt=auth.SESSION_SALT).dumps(
        {"v": 99}
    )
    assert auth.SessionStore(secret=SECRET).verify(token) is False


def _signed_seconds_ago(
    secret: bytes, salt: str, seconds: int, generation: int = 0
) -> str:
    """A token whose timestamp is `seconds` in the past, signed with the same secret.

    The generation is the caller's rather than baked in, because a token minted without one
    is refused for a *second* reason and the age assertion would pass for the wrong one.
    """
    from itsdangerous import URLSafeTimedSerializer
    from itsdangerous.timed import TimestampSigner

    real = TimestampSigner.get_timestamp
    try:
        TimestampSigner.get_timestamp = lambda self: int(real(self)) - seconds
        return URLSafeTimedSerializer(secret, salt=salt).dumps(
            {**auth.TOKEN_PAYLOAD, "g": generation}
        )
    finally:
        TimestampSigner.get_timestamp = real


# --------------------------------------------------------------------------- #
# The cookie
# --------------------------------------------------------------------------- #
def test_the_cookie_is_httponly_and_samesite_lax():
    """`HttpOnly` keeps the cookie out of `document.cookie`, so an XSS cannot replay it.

    `SameSite=Lax` is what stops a third-party page from driving an authenticated POST: it
    keeps the cookie off cross-site form submissions, which is the whole CSRF surface here
    (`POST /api/jobs` enqueues work, `DELETE /api/library/files/{id}` removes files).
    """
    assert auth.COOKIE_HTTPONLY is True
    assert auth.COOKIE_SAMESITE == "lax"
    assert auth.COOKIE_NAME == "amd_hub_session"


def test_the_cookie_is_secure_only_under_tls():
    """`Secure` is set from the request, not from config.

    A hardcoded `Secure` would make the cookie undeliverable over plain HTTP -- the LAN
    deployment the image is built for -- and a hardcoded *absence* of it would hand the
    session to anyone on a coffee-shop wifi. So the attribute follows what the request
    actually was.
    """
    assert auth.is_tls_request(_Request("https")) is True
    assert auth.is_tls_request(_Request("http")) is False
    # Behind a TLS-terminating reverse proxy the ASGI scope says `http`; the forwarded
    # header is the only evidence the client saw HTTPS -- but only once an operator says a
    # proxy is in front, which is the second assertion's job.
    assert auth.is_tls_request(_Request("http", forwarded_proto="https")) is False
    assert (
        auth.is_tls_request(_Request("http", forwarded_proto="https"), trust_forwarded=True)
        is True
    )
    assert (
        auth.is_tls_request(
            _Request("http", forwarded_proto="http, http"), trust_forwarded=True
        )
        is False
    )
    assert (
        auth.is_tls_request(_Request("https", forwarded_proto="http"), trust_forwarded=True)
        is True
    )
    # A *disagreeing* chain. The leftmost element is RFC 7239's original protocol -- what the
    # browser spoke to the outermost proxy -- and each hop appends what it saw afterwards, so
    # `"https, http"` is a browser on HTTPS whose last hop saw plaintext *after* a trusted
    # proxy re-originated. The answer is TLS, and it is the answer that keeps `Secure` on.
    # The docstring previously said the opposite of the code; this is what says what it does.
    assert (
        auth.is_tls_request(
            _Request("http", forwarded_proto="https, http"), trust_forwarded=True
        )
        is True
    )
    assert (
        auth.is_tls_request(
            _Request("http", forwarded_proto="http, https"), trust_forwarded=True
        )
        is False
    )
    assert (
        auth.is_tls_request(
            _Request("http", forwarded_proto="HTTPS, HTTP"), trust_forwarded=True
        )
        is True
    )


def test_trust_forwarded_has_no_call_site_and_the_docstring_says_so():
    """A parameter that reads as configurable and cannot be configured is a lie.

    `trust_forwarded` is keyword-only with no default that anything sets, no environment
    variable, and no setting. The docstring used to present it as "an operator decision" --
    an operator has no way to make it. So this asserts two things: that the docstring says
    the supported deployment (TLS at uvicorn, or no proxy), and that the only call sites pass
    it positionally-false, so a future call site has to *choose* to enable it rather than
    inherit it.
    """
    import importlib
    import inspect

    from hub import api

    assert "Terminate TLS at uvicorn" in inspect.getdoc(auth.is_tls_request)

    # The module is imported by name rather than read off `api.auth`, because `api/__init__.py`
    # deliberately does *not* bind the name `auth` on itself (binding it would shadow the
    # `api/auth.py` submodule and break `from hub.api import auth`).
    router_module = importlib.import_module("hub.api.auth")
    sources = [
        Path(api.__file__).read_text(encoding="utf-8"),
        Path(router_module.__file__).read_text(encoding="utf-8"),
    ]
    # Two call sites -- set the cookie, clear it -- and no `trust_forwarded=True` anywhere, so
    # the cookie's `Secure` follows the ASGI scheme alone in every deployment this build can
    # produce. A future call site has to choose to pass it, which is the point.
    assert sum(source.count("is_tls_request(") for source in sources) >= 2
    assert "trust_forwarded=True" not in "".join(sources)


def test_tls_detection_ignores_a_spoofed_forwarded_header_on_a_plain_request():
    """`X-Forwarded-Proto` is only believed when a proxy is declared to be in front.

    Without this, any client could send `X-Forwarded-Proto: https` to talk the hub out of
    setting `Secure` -- which is not a way to *steal* a cookie, but it is a way to make the
    hub silently downgrade its own protection, and the fix is one keyword.
    """
    assert (
        auth.is_tls_request(_Request("http", forwarded_proto="https"), trust_forwarded=False)
        is False
    )


class _Request:
    """The two attributes `is_tls_request` reads, and nothing else."""

    def __init__(self, scheme: str, forwarded_proto: str | None = None) -> None:
        self.url = type("URL", (), {"scheme": scheme})()
        self.headers = (
            {}
            if forwarded_proto is None
            else {"x-forwarded-proto": forwarded_proto}
        )


def test_a_short_secret_is_refused_at_construction():
    """`load_settings` already refuses a short `AMD_SESSION_SECRET`; this is the second lock.

    An HMAC keyed with a short secret is brute-forceable offline, and a session cookie is the
    one artefact in the system whose forgery is silent -- the forged token grants a session
    and nothing anywhere reports it.
    """
    with pytest.raises(ValueError, match="session secret"):
        auth.SessionStore(secret=b"short")


def test_the_rate_limiter_uses_a_monotonic_clock_by_default():
    """`time.monotonic` and not `time.time`.

    A wall-clock limiter is unlockable: NTP steps the clock back an hour and every locked-out
    address is admitted again. The event loop's own clock is what `asyncio` uses for deadlines
    and is the right one here for the same reason.
    """
    store = auth.SessionStore(secret=SECRET)
    assert store.check_rate_limit("10.0.0.1") is None
    assert store.retry_after("10.0.0.1") == 0.0  # one attempt, max_attempts is 10
    assert isinstance(time.monotonic(), float)
