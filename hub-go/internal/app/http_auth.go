package app

import (
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"amdhub/internal/auth"
)

// handleLogin is the JSON login, and the same decision as the form's.
//
// Rate limit first, then the comparison, then the budget back. The order is the
// design: the limit is the only thing bounding guesses, because the comparison is
// constant-time and therefore no slower for a near-miss than for a wild one.
//
// **A successful login is followed by a restart of the wrapper**, because the payload
// reads its token cache at process start: a wrapper that was already serving when the
// account was logged into keeps serving with the old -- or no -- account. Reporting
// success on the login call alone would leave a serving-but-unauthenticated wrapper:
// `regions` stays empty, every download returns nothing, and the page says "logged in".
// So the restart happens before the answer, and success is reported only once a
// restarted wrapper actually reports regions.
func (s *State) handleLogin(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	decodeBody(r, &body)
	ok, failure := s.authenticate(r, body.Password)
	if failure != nil {
		fail(w, http.StatusTooManyRequests, failure.Error(),
			map[string]any{"retry_after": int(max(1, failure.RetryAfter.Seconds()))})
		return
	}
	if !ok {
		fail(w, http.StatusUnauthorized, auth.LoginFailed)
		return
	}
	s.Sessions.SetSessionCookie(w, r, time.Now(), s.SessionGeneration(), s.Settings.TrustForwarded)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleLoginPage renders the form. Reachable with or without a session, so a
// logged-in user can get back to it.
func (s *State) handleLoginPage(w http.ResponseWriter, r *http.Request) {
	view := loginView{
		baseView:  s.baseView(r, "Sign in · amd-hub"),
		Failed:    r.URL.Query().Get("error") != "",
		Message:   auth.LoginFailed,
		WrapperOK: fileExists(s.Settings.WrapperBinary),
		Binary:    s.Settings.WrapperBinary,
	}
	s.render(w, "login", view)
}

// handleLoginForm is the form's target, and a no-JS path to a session.
//
// **A failure redirects rather than rendering a 401**, because a browser navigating to
// a 401 gets a JSON body in the place of a page. The error is a query flag and not a
// message: the page renders `auth.LoginFailed` itself, so nothing about the attempt is
// carried in a URL that ends up in history and in the `Referer` of the next request.
func (s *State) handleLoginForm(w http.ResponseWriter, r *http.Request) {
	form := readForm(r)
	ok, failure := s.authenticate(r, form["password"])
	if failure != nil {
		http.Redirect(w, r, LoginPath+"?error=1", http.StatusSeeOther)
		return
	}
	if !ok {
		http.Redirect(w, r, LoginPath+"?error=1", http.StatusSeeOther)
		return
	}
	s.Sessions.SetSessionCookie(w, r, time.Now(), s.SessionGeneration(), s.Settings.TrustForwarded)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// handleLogout drops the session and retires the generation, which is the half that
// actually revokes anything.
func (s *State) handleLogout(w http.ResponseWriter, r *http.Request) {
	s.RetireSession()
	http.SetCookie(w, &http.Cookie{
		Name:     auth.CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: auth.CookieHTTPOnly,
		SameSite: http.SameSiteLaxMode,
		Secure:   auth.IsTLSRequest(r, s.Settings.TrustForwarded),
	})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleLogoutPage is the page's logout, and it is **unauthenticated on purpose**.
//
// A cross-site logout -- a page anywhere posting a form at `<hub>/logout` -- is a
// nuisance, not a breach: it revokes a session the user already had and sends them to
// a login form. Guarding it would mean a cross-site form could not log you out but
// *could* have done nothing else useful, and the cost is that a browser following a
// redirect with a stale cookie would get a 401 as a login form instead of a redirect.
func (s *State) handleLogoutPage(w http.ResponseWriter, r *http.Request) {
	s.RetireSession()
	http.SetCookie(w, &http.Cookie{
		Name:     auth.CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: auth.CookieHTTPOnly,
		SameSite: http.SameSiteLaxMode,
		Secure:   auth.IsTLSRequest(r, s.Settings.TrustForwarded),
	})
	http.Redirect(w, r, LoginPath, http.StatusSeeOther)
}

// handleSession reports whether there is a session, which a client uses to decide
// whether to render a login form.
func (s *State) handleSession(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"authenticated": s.authenticated(r)})
}

// authenticate is the whole login decision, for both routes. `ok` on success, or the
// rate-limit error that has to be answered differently.
func (s *State) authenticate(r *http.Request, password string) (bool, *auth.RateLimitedError) {
	ip := auth.ClientIP(r)
	if err := s.Sessions.CheckRateLimit(ip, time.Now()); err != nil {
		var limited *auth.RateLimitedError
		if ok := asRateLimited(err, &limited); ok {
			return false, limited
		}
		return false, nil
	}
	if !auth.VerifyPassword(password, s.Settings.Password) {
		return false, nil
	}
	// An operator who has just mistyped nine times and then got it right should not be
	// one attempt away from a lockout.
	s.Sessions.Forget(ip)
	return true, nil
}

func asRateLimited(err error, target **auth.RateLimitedError) bool {
	limited, ok := err.(*auth.RateLimitedError)
	if ok {
		*target = limited
	}
	return ok
}

// handleStatic serves the two public assets and everything else behind the session.
func (s *State) handleStatic(w http.ResponseWriter, r *http.Request, params map[string]string) {
	served := "/static/" + strings.TrimPrefix(params["file"], "/")
	if !PublicStatic[served] && !s.authenticated(r) {
		fail(w, http.StatusUnauthorized, "this asset needs a session.")
		return
	}
	asset, ok := staticAssets[strings.TrimPrefix(served, "/static/")]
	if !ok {
		fail(w, http.StatusNotFound, "no such asset.")
		return
	}
	if strings.HasSuffix(served, ".css") {
		w.Header().Set("Content-Type", "text/css; charset=utf-8")
	} else {
		w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
	}
	_, _ = w.Write([]byte(asset))
}

// jsonBody is a small helper for the handlers that need to report the parsed body in
// an error message.
func jsonBody(raw []byte, out any) error { return json.Unmarshal(raw, out) }

func max(first, second float64) float64 {
	if first > second {
		return first
	}
	return second
}
