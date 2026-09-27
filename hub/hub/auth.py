"""One password, one signed cookie, one rate limit (spec §11).

Everything in this module is small and every line of it is a security decision, so the
docstrings say *why* rather than what. The four properties spec §11 asks for, and where each
one is pinned:

| §11 | here | pinned by |
|-----|------|-----------|
| a single password, never hardcoded | `verify_password` | `test_password_compared_in_constant_time` |
| `secrets.compare_digest` | `verify_password` | the same test, by asserting the call |
| 10 attempts per 5 minutes | `SessionStore.check_rate_limit` | `test_rate_limit_blocks_after_ten_attempts` |
| `HttpOnly`, `SameSite=Lax`, `Secure` under TLS | `COOKIE_*`, `is_tls_request` | `test_the_cookie_is_*` |

**No server-side session table.** `itsdangerous.URLSafeTimedSerializer` puts an HMAC in the
cookie and the verification is a signature check, so `restarting the hub logs everyone out`
costs nothing and there is no store to keep in sync with the password. The cost of that
choice is the other half: a token issued under an old password stays valid until it ages
out, which is why `max_age` is a real bound and not a formality.

**A fresh `AMD_SESSION_SECRET` per process invalidates every session, and that is the
intended behaviour.** `load_settings` generates one when the variable is unset, precisely so
that no secret ships in the image; a deployment that wants its logins to survive a restart
sets the variable.

**`RateLimited` is raised by `check_rate_limit` *before* the password is looked at, and
`check_rate_limit` records the attempt.** That ordering is what makes the limit useful: the
comparison is constant-time, so an attacker who cannot see a timing signal would otherwise be
limited only by the network. Counting the attempt before the answer means ten wrong guesses
cost ten slots whatever the guesses were.

**`is_tls_request` trusts `X-Forwarded-Proto` only when the operator says a proxy is in
front** (`trust_forwarded`, off by default). The header is trivially spoofable by any client,
so believing it unconditionally would let anyone talk the hub out of setting `Secure` on its
own session cookie -- not a way to steal one, but a way to make the hub silently downgrade
its own protection. The direct scheme is always believed, because it is set by the server.
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import Callable

import itsdangerous

# The cookie name. Prefixed so it cannot collide with anything the upstream client or a
# reverse proxy in front of the hub might set on the same host.
COOKIE_NAME = "amd_hub_session"

COOKIE_HTTPONLY = True
COOKIE_SAMESITE = "lax"
# Long enough for a laptop that sleeps through a night, short enough that a stolen cookie
# stops working the next day. Not a rotation scheme: a rotation needs a server-side table,
# and §11 does not ask for one.
DEFAULT_MAX_AGE = 12 * 60 * 60

# spec §11: "ログイン試行 レートリミット（既定 10 回 / 5 分）".
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_WINDOW = 300.0

# How many addresses the limiter remembers. A backstop, not the limit itself: the entries are
# pruned as their windows move past, and this only bounds the worst case where every address
# in the sweep is still inside its window. Reachable from a scan, so it is a real number.
_MAX_TRACKED_IPS = 4096

# Sweep when the dict passes this, not on every call. See `_prune` for why the threshold is
# a third over the cap rather than the cap itself: it is what makes the sweep amortised
# instead of per-request, and a sweep is O(addresses).
_PRUNE_THRESHOLD = _MAX_TRACKED_IPS + _MAX_TRACKED_IPS // 3

# The salting. Its own name rather than `"session"` so that a token minted by some other
# `URLSafeTimedSerializer` sharing this process's secret -- nothing does today -- is not
# accepted as a session.
SESSION_SALT = "amd-hub.session.v1"

# The payload is a version and nothing else: a session is a boolean, and anything a caller
# could read out of it would be a place for a claim to be forged alongside the signature.
TOKEN_PAYLOAD = {"v": 1}

# One message for every authentication failure, and the only one the login handler is allowed
# to produce. A route that said "no such user" and "wrong password" separately is telling an
# attacker which half to work on; §11's single shared password means there is only one half,
# so there is only one answer. It must not echo the submitted value either: that turns the
# login form into a reflection point.
LOGIN_FAILED = "Wrong password."


class RateLimited(Exception):
    """Too many login attempts from one address inside the window.

    `retry_after` is a real number of seconds rather than a constant, because it is what the
    response tells the client, and a `Retry-After: 0` would invite the client straight back
    into the route it was just refused.
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"too many login attempts; try again in {retry_after:.0f}s"
        )


def verify_password(candidate: object, expected: str) -> bool:
    """Whether `candidate` is the shared password, compared in constant time.

    `secrets.compare_digest` and not `==`. `==` on two `str` returns as soon as the first
    differing character is found, and the time it took is the number of leading characters
    that were right -- which is the whole of a six-to-twelve character password, recovered
    one character at a time from a few thousand requests.

    A `candidate` that is not a `str` is refused rather than passed on. `compare_digest`
    raises `TypeError` when its two arguments are of different types, and the only caller
    that matters parses a request body: a `{"password": 5}` is a 500 on the one route a
    stranger can reach, for no gain.
    """
    if not isinstance(candidate, str):
        return False
    return bool(secrets.compare_digest(candidate, expected))


def is_tls_request(request, *, trust_forwarded: bool = False) -> bool:
    """Whether this request arrived over TLS, for the cookie's `Secure` attribute.

    The ASGI scheme is the server's own answer and is always believed. The
    `X-Forwarded-Proto` header is a *client-supplied string* and is only read when
    `trust_forwarded` says a TLS-terminating proxy is in front, because behind one the
    scheme the ASGI server sees is `http` no matter what the browser negotiated.

    The leftmost value is taken because it is RFC 7239's *original protocol*: each proxy in a
    chain appends, so the first element is what the browser actually spoke and the rest are
    what each hop saw afterwards. A disagreeing chain (`"https, http"`) is therefore HTTPS,
    which is the direction that keeps the attribute on.

    `trust_forwarded` has no call site and no setting, and this is the one place that says so
    rather than leaving a parameter that reads as configurable. **Terminate TLS at uvicorn,
    or do not terminate it in front of the hub at all.** Behind a TLS-terminating proxy the
    ASGI scope says `http` whatever the browser negotiated, so the cookie would be issued
    without `Secure`; that is the failure this parameter exists to make impossible, and
    wiring it to a setting is Phase 2's work if a deployment ever needs it. What is not
    acceptable is the current shape, where the parameter's existence implies an operator
    decision that nothing can actually make.
    """
    scheme = getattr(getattr(request, "url", None), "scheme", "") or ""
    if scheme.lower() == "https":
        return True
    if not trust_forwarded:
        return False
    headers = getattr(request, "headers", None) or {}
    forwarded = headers.get("x-forwarded-proto") or ""
    # Leftmost: RFC 7239's original protocol, i.e. what the browser spoke to the outermost
    # proxy. See the docstring.
    return forwarded.split(",")[0].strip().lower() == "https"


class SessionStore:
    """Issues and verifies session tokens, and rate-limits the login that mints them.

    One instance per process, on `app.state`. Not a singleton by design -- two of these in one
    process would each issue tokens the other could not read, and a test that wanted to
    exercise a limit would have to reach into module state to get one.
    """

    def __init__(
        self,
        *,
        secret: bytes,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window: float = DEFAULT_WINDOW,
        max_age: int = DEFAULT_MAX_AGE,
        salt: str = SESSION_SALT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # A short HMAC key is brute-forceable offline and a forged session is silent: nothing
        # anywhere reports it, the cookie simply works. `load_settings` refuses a short
        # `AMD_SESSION_SECRET` already; this is the same rule at the point of use, because
        # a `SessionStore` can be built by anything.
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
            raise ValueError(
                f"the session secret must be at least {MIN_SECRET_BYTES} bytes, got "
                f"{len(secret) if isinstance(secret, (bytes, bytearray)) else type(secret).__name__}"
            )
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts!r}")
        if window <= 0:
            raise ValueError(f"window must be positive, got {window!r}")

        self._serializer = itsdangerous.URLSafeTimedSerializer(bytes(secret), salt=salt)
        self._max_attempts = max_attempts
        self._window = float(window)
        self._max_age = int(max_age)
        self._clock = clock
        # ip -> deque of attempt timestamps. A `deque` because every call both appends and
        # discards from the left, and that is the only thing it is ever asked to do.
        self._attempts: dict[str, deque[float]] = {}

    # -- tokens -------------------------------------------------------------

    def issue(self, generation: int = 0) -> str:
        """A fresh signed token, carrying `generation`. The only way a session begins.

        `generation` is the revocation mechanism, and it is in the *signed* payload rather
        than in a server-side table because a session here is a boolean: there is nothing else
        about it to store, so the whole of "which sessions are alive" is one integer the
        store compares against. Bumping it invalidates every token ever issued, which is what
        logout needs.

        **Logout is the only thing that bumps it today.** A previous version of this docstring
        also said a password change would, and there is no password-change route: the
        password is a single value read from the environment at start-up, so there is no
        endpoint and nothing to invalidate at the moment of a change -- a changed password
        takes effect at the next restart, and the container image is where it is rotated. The
        claim is deleted rather than kept as a promise, because a comment asserting a security
        property the code does not have is worse than no comment: a reader would design
        around it. If a change-password route is ever added, the one line needed is
        `app.state.session_generation = app.state.sessions.retire(current)`, and the
        contract to test is "the old token is refused afterwards".
        """
        return self._serializer.dumps({**TOKEN_PAYLOAD, "g": int(generation)})

    def verify(self, token: object, generation: int = 0) -> bool:
        """Whether `token` is a live session: signed, unexpired, and of this generation.

        Never raises. It is called on every request against a cookie the client chose, so a
        `TypeError` from a `None` cookie or a `BadSignature` from an edited one are both
        ordinary "no", not exceptions. An unauthenticated 500 would also tell a prober that
        the guard ran, and put a traceback on a public route.

        **`generation` is compared, not merely carried.** A token that verifies its signature
        but names a retired generation is a session that has been logged out, and returning
        `True` for it is what made `POST /api/auth/logout` a suggestion: the browser dropped
        the cookie, and anyone who had copied the token -- which is the entire threat model
        for a bearer cookie -- kept it. `test_a_token_from_a_retired_generation_is_refused`
        is the one that fails if the comparison is dropped.
        """
        if not isinstance(token, str) or not token:
            return False
        try:
            payload = self._serializer.loads(token, max_age=self._max_age)
        except itsdangerous.BadSignature:
            # `SignatureExpired` is a subclass, so an aged-out token is refused here too --
            # which is the point of `max_age` being a bound rather than a suggestion.
            return False
        except Exception:  # noqa: BLE001 - a malformed token is "no", never a crash
            return False
        if not isinstance(payload, dict) or payload.get("v") != TOKEN_PAYLOAD["v"]:
            return False
        # `isinstance(..., int)` and not `bool` before the equality, because `True == 1` in
        # Python and a payload carrying a JSON `true` would otherwise verify as generation 1.
        # The signature makes forging one impossible in practice; this removes the question
        # for a cost of one line.
        stamped = payload.get("g")
        if isinstance(stamped, bool) or not isinstance(stamped, int):
            return False
        return stamped == int(generation)

    def retire(self, generation: int) -> int:
        """Retire `generation` and return the new one. `generation + 1`, always moving on.

        A monotonic counter rather than a random one so that the value is comparable and
        debuggable, and always incremented rather than set to a fresh random value so that
        two logouts racing cannot hand two different tokens the same generation.
        """
        return int(generation) + 1

    # -- the login limit ----------------------------------------------------

    def check_rate_limit(self, ip: str) -> None:
        """Take an attempt slot for `ip`, or raise `RateLimited` if the window is full.

        Checks *and* records, in that order, and that is why the brief's test calls it ten
        times before expecting a refusal: the tenth call is the one that fills the window, so
        the eleventh is the first that is turned away. Recording before the password is
        compared is deliberate -- the comparison is constant-time, so the count is the only
        thing standing between a stranger and an unbounded number of guesses.
        """
        now = self._clock()
        stamps = self._attempts.setdefault(ip, deque())
        while stamps and now - stamps[0] >= self._window:
            stamps.popleft()
        if len(stamps) >= self._max_attempts:
            raise RateLimited(self._retry_after(stamps, now))
        stamps.append(now)
        self._prune(now)

    def forget(self, ip: str) -> None:
        """Give `ip` its whole budget back, after a successful login.

        Without this, ten *correct* logins would lock the owner out -- which is what happens
        to anyone who logs in again after every hub restart, or who has two browser tabs.
        """
        self._attempts.pop(ip, None)

    def retry_after(self, ip: str) -> float:
        """Seconds until `ip` could try again, or `0.0` if it can try now."""
        stamps = self._attempts.get(ip)
        if not stamps:
            return 0.0
        return self._retry_after(stamps, self._clock())

    def tracked_ips(self) -> int:
        """How many addresses are currently remembered. Read-only, and for tests.

        The same reasoning as `EventBroker.subscriber_count`: the pruning in `_prune` has no
        observable consequence from outside, so this is the assertion surface for it.
        """
        return len(self._attempts)

    def _retry_after(self, stamps: deque[float], now: float) -> float:
        """When the oldest attempt in the window leaves it."""
        if len(stamps) < self._max_attempts:
            return 0.0
        return max(0.0, min(self._window, stamps[0] + self._window - now))

    def _prune(self, now: float) -> None:
        """Drop every window that has moved past, and enforce the address cap.

        **Amortised, not per call** (M4). This used to walk the whole dict on every attempt,
        inside an `async` handler, so a sweep from a thousand addresses cost a thousand O(n)
        passes on the event loop -- the one thread everything else needs. The rule now is
        "when the dict is a third over the cap, sweep it", which is the standard amortised
        form: after a sweep the size is at most the cap, and the next sweep is therefore at
        least two-thirds-of-a-cap attempts away, so each entry is visited O(1) times on
        average and no single request pays for the whole table.

        `tracked_ips()` is the observable, and `test_the_limiter_sweeps_amortisedly` holds the
        bound rather than the implementation: a caller that restores the per-call sweep will
        fail it, and one that sweeps more often will too.
        """
        if len(self._attempts) <= _PRUNE_THRESHOLD:
            return

        stale = [
            ip
            for ip, stamps in self._attempts.items()
            if not stamps or now - stamps[-1] >= self._window
        ]
        for ip in stale:
            del self._attempts[ip]

        overflow = len(self._attempts) - _MAX_TRACKED_IPS
        if overflow > 0:
            # The oldest *first-attempt* entries, which are the closest to freeing their
            # slots. `sorted` on the first timestamp is deterministic, so the eviction is not
            # a function of dict insertion luck.
            oldest = sorted(self._attempts.items(), key=lambda item: item[1][0])
            for ip, _ in oldest[:overflow]:
                del self._attempts[ip]


# Kept next to the check rather than next to the constant so that the number in the error
# message and the number in the condition cannot drift apart.
MIN_SECRET_BYTES = 32
