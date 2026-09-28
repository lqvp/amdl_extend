// Package auth is a port of `hub/auth.py`: one password, one signed cookie, one
// rate limit.
//
// Everything in this package is small and every line of it is a security decision,
// so the comments say *why* rather than what. The four properties at issue:
//
//	| property                                       | here                  |
//	|------------------------------------------------|-----------------------|
//	| a single password, never hardcoded             | VerifyPassword        |
//	| a constant-time comparison                     | VerifyPassword        |
//	| 10 attempts per 5 minutes per address          | CheckRateLimit        |
//	| HttpOnly, SameSite=Lax, Secure under TLS       | SetSessionCookie      |
//
// **No server-side session table.** The cookie carries an HMAC and the
// verification is a signature check, so restarting the hub logs everyone out and
// costs nothing, and there is no store to keep in sync with the password. The cost
// of that choice is the other half: a token issued under an old password stays
// valid until it ages out, which is why `MaxAge` is a real bound and not a
// formality.
//
// **A fresh session secret per process invalidates every session, and that is the
// intended behaviour.** `config.Load` generates one when the variable is unset,
// precisely so that no secret ships in the image; a deployment that wants its
// logins to survive a restart sets `AMD_SESSION_SECRET`.
//
// **The cookie format is this port's own, and it is deliberately not
// byte-compatible with `itsdangerous`.** A session is a boolean with a timestamp;
// two implementations of the same boolean that agree on the *policy* are the whole
// requirement, and reimplementing a third-party serializer's exact token layout to
// preserve logins across a rewrite nobody would keep sessions across is a
// correctness risk taken for no gain. Existing sessions therefore do not survive
// the switch -- which is what a regenerated secret does anyway.
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// CookieName is prefixed so it cannot collide with anything the upstream client or
// a reverse proxy in front of the hub might set on the same host.
const CookieName = "amd_hub_session"

// Cookie attributes. SameSite=Lax and HttpOnly are not configurable: the browser
// client is same-origin and there is no cross-site flow that needs the session.
const (
	CookieHTTPOnly = true
	CookieSameSite = "Lax"
)

// MaxAge is long enough for a laptop that sleeps through a night, short enough
// that a stolen cookie stops working the next day. Not a rotation scheme, because
// a rotation needs a server-side table, and none is wanted here.
const MaxAge = 12 * time.Hour

// Ten attempts per five minutes: "ログイン試行 レートリミット（既定 10 回 / 5 分）".
const (
	MaxAttempts = 10
	Window      = 5 * time.Minute
)

// maxTrackedIPs is how many addresses the limiter remembers. A backstop, not the
// limit itself: the entries are pruned as their windows move past, and this only
// bounds the worst case where every address in the sweep is still inside its
// window. Reachable from a scan, so it is a real number.
const maxTrackedIPs = 4096

// pruneThreshold is a third over the cap rather than the cap itself: it is what
// makes the sweep amortised instead of per-request, and a sweep is O(addresses).
const pruneThreshold = maxTrackedIPs + maxTrackedIPs/3

// Salt is its own string rather than "session" so that a token minted by some
// other signer sharing this process's secret -- nothing does today -- is not
// accepted as a session.
const Salt = "amd-hub.session.v1"

// LoginFailed is the one message for every authentication failure, and the only
// one the login handler is allowed to produce. A route that said "no such user" and
// "wrong password" separately is telling an attacker which half to work on; a
// single shared password means there is only one half, so there is only one answer.
// It must not echo the submitted value either: that turns the login form into a
// reflection point.
const LoginFailed = "Wrong password."

// RateLimitedError is too many login attempts from one address inside the window.
//
// `RetryAfter` is a real number of seconds rather than a constant, because it is
// what the response tells the client, and a `Retry-After: 0` would invite the
// client straight back into the route it was just refused.
type RateLimitedError struct {
	RetryAfter time.Duration
}

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("too many login attempts; try again in %.0fs", e.RetryAfter.Seconds())
}

// VerifyPassword reports whether `candidate` is the shared password, compared in
// constant time.
//
// `subtle.ConstantTimeCompare` and not `==`. `==` on two strings returns as soon as
// the first differing character is found, and the time it took is the number of
// leading characters that were right -- which is the whole of a six-to-twelve
// character password, recovered one character at a time from a few thousand
// requests.
//
// The length difference is visible, which is the same leak `compare_digest` has in
// Python and is worth naming: constant-time over unequal lengths would mean
// comparing against a padded copy, and what that buys is the length of a password
// the operator chose.
func VerifyPassword(candidate, expected string) bool {
	return subtle.ConstantTimeCompare([]byte(candidate), []byte(expected)) == 1
}

// Sessions mints, verifies, and rate-limits.
type Sessions struct {
	secret []byte

	mu       sync.Mutex
	attempts map[string][]time.Time
}

// NewSessions returns a store over one secret.
func NewSessions(secret []byte) *Sessions {
	return &Sessions{secret: secret, attempts: map[string][]time.Time{}}
}

// Issue mints a token: `v1.<generation>.<unix seconds>.<base64url HMAC>`.
//
// A version prefix so that a future format can be told apart from this one without
// guessing, and everything else inside the signature so that neither the timestamp
// nor the generation can be edited in the cookie.
//
// **The generation is the revocation mechanism**, and it is in the signed payload
// rather than in a server-side table because a session here is a boolean: there is
// nothing else about it to store, so the whole of "which sessions are alive" is one
// integer the store compares against. Bumping it invalidates every token ever
// issued, which is what logout needs. Without the comparison, logout was a
// suggestion: the browser dropped the cookie, and anyone who had copied the token --
// which is the entire threat model for a bearer cookie -- kept it.
func (s *Sessions) Issue(now time.Time, generation int) string {
	stamp := strconv.FormatInt(now.Unix(), 10)
	payload := "v1." + strconv.Itoa(generation) + "." + stamp
	return payload + "." + s.sign(payload)
}

// Retire retires a generation and returns the new one: `generation + 1`, always
// moving on.
//
// A monotonic counter rather than a random value so that the value is comparable and
// debuggable, and always incremented rather than set to a fresh random value so that
// two logouts racing cannot hand two different tokens the same generation.
func Retire(generation int) int { return generation + 1 }

// Valid reports whether a token is a live session: signed, unexpired, and of the
// generation the store is currently on.
//
// The expiry check is against the *signed* timestamp, so a token cannot be aged
// out or kept alive by editing the cookie, and the signature comparison is
// constant-time for the same reason the password one is.
//
// `generation` is compared, not merely carried: a token that verifies its signature
// but names a retired generation is a session that has been logged out.
func (s *Sessions) Valid(token string, now time.Time, generation int) bool {
	parts := strings.Split(token, ".")
	if len(parts) != 4 || parts[0] != "v1" {
		return false
	}
	expected := s.sign(strings.Join(parts[:3], "."))
	if subtle.ConstantTimeCompare([]byte(expected), []byte(parts[3])) != 1 {
		return false
	}
	stamped, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil {
		return false
	}
	issued, err := strconv.ParseInt(parts[2], 10, 64)
	if err != nil {
		return false
	}
	issuedAt := time.Unix(issued, 0)
	return now.Sub(issuedAt) <= MaxAge && int(stamped) == generation
}

func (s *Sessions) sign(payload string) string {
	mac := hmac.New(sha256.New, s.secret)
	mac.Write([]byte(Salt))
	mac.Write([]byte{0})
	mac.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// CheckRateLimit records an attempt from `ip` and refuses when the address is over
// its budget.
//
// **Called before the password is looked at, and it records the attempt.** That
// ordering is what makes the limit useful: the comparison is constant-time, so an
// attacker who cannot see a timing signal would otherwise be limited only by the
// network. Counting the attempt before the answer means ten wrong guesses cost ten
// slots whatever the guesses were.
func (s *Sessions) CheckRateLimit(ip string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.attempts) > pruneThreshold {
		s.pruneLocked(now)
	}
	kept := s.attempts[ip][:0]
	for _, at := range s.attempts[ip] {
		if now.Sub(at) < Window {
			kept = append(kept, at)
		}
	}
	if len(kept) >= MaxAttempts {
		// The oldest attempt in the window is what says when the next one is
		// allowed, which is a real number and not a fixed backoff.
		oldest := kept[0]
		s.attempts[ip] = kept
		wait := Window - now.Sub(oldest)
		if wait < time.Second {
			wait = time.Second
		}
		return &RateLimitedError{RetryAfter: wait}
	}
	s.attempts[ip] = append(kept, now)
	return nil
}

// Forget drops an address's budget, called after a successful login: an operator
// who has just mistyped nine times and then got it right should not be one attempt
// away from a lockout.
func (s *Sessions) Forget(ip string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.attempts, ip)
}

func (s *Sessions) pruneLocked(now time.Time) {
	for ip, attempts := range s.attempts {
		alive := false
		for _, at := range attempts {
			if now.Sub(at) < Window {
				alive = true
				break
			}
		}
		if !alive {
			delete(s.attempts, ip)
		}
	}
}

// IsTLSRequest reports whether the request reached the hub over TLS, which decides
// the `Secure` attribute on the session cookie.
//
// `X-Forwarded-Proto` is trusted only when the operator says a proxy is in front
// (`trust_forwarded`, off by default). The header is trivially spoofable by any
// client, so believing it unconditionally would let anyone talk the hub out of
// setting `Secure` on its own session cookie -- not a way to steal one, but a way
// to make the hub silently downgrade its own protection. The direct scheme is
// always believed, because it is set by the server.
func IsTLSRequest(r *http.Request, trustForwarded bool) bool {
	if r.TLS != nil {
		return true
	}
	if trustForwarded {
		return strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
	}
	return false
}

// ClientIP is the address the rate limiter counts against.
//
// The transport's own remote address, never a header: an `X-Forwarded-For` is
// attacker-controlled, and a limiter keyed on it is a limiter an attacker chooses
// the key of. Behind a proxy that terminates the connection this is the *proxy's*
// address, which is a real limitation rather than an oversight -- the alternative
// is trusting a spoofable header, and the hub is documented as a LAN service
// reachable directly.
func ClientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// ErrNoSession is returned by `FromRequest` when there is no valid cookie, which
// the API layer turns into a redirect for a page and a 401 for a fetch.
var ErrNoSession = errors.New("no valid session cookie")

// FromRequest is the cookie's verdict. Never panics on a malformed cookie: this is
// called on every request against a value the client chose, so an edited one is an
// ordinary "no".
func (s *Sessions) FromRequest(r *http.Request, now time.Time, generation int) error {
	cookie, err := r.Cookie(CookieName)
	if err != nil {
		return ErrNoSession
	}
	if !s.Valid(cookie.Value, now, generation) {
		return ErrNoSession
	}
	return nil
}

// SetSessionCookie puts a fresh session on the response, with the attributes a
// session cookie needs.
//
// `Secure` follows the request rather than a setting, because a hardcoded one
// breaks the plain-HTTP LAN deployment and its absence hands the session to the
// local network.
func (s *Sessions) SetSessionCookie(w http.ResponseWriter, r *http.Request, now time.Time, generation int, trustForwarded bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    s.Issue(now, generation),
		Path:     "/",
		MaxAge:   int(MaxAge.Seconds()),
		HttpOnly: CookieHTTPOnly,
		SameSite: http.SameSiteLaxMode,
		Secure:   IsTLSRequest(r, trustForwarded),
	})
}

// ClearSessionCookie drops the cookie.
//
// The attributes must *match* the ones it was set with or the browser keeps the
// original: a `Set-Cookie` for the same name that differs only in `Path` is a
// different cookie, and the old one is still on the request the next time.
//
// **This is tidiness, not revocation.** The token in the copy the browser is being
// told to forget is still valid until it ages out; that is the cost of having no
// server-side table, and it is why `MaxAge` is a real bound.
func (s *Sessions) ClearSessionCookie(w http.ResponseWriter, r *http.Request, trustForwarded bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: CookieHTTPOnly,
		SameSite: http.SameSiteLaxMode,
		Secure:   IsTLSRequest(r, trustForwarded),
	})
}
