package app

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"amdhub/internal/jobs"
)

// SecurityHeaders are sent with every response. Defence in depth, and one of the
// three is a *correctness* requirement rather than a nicety:
//
//   - `X-Content-Type-Options: nosniff` stops a browser from re-interpreting a
//     response as a type the server did not send. The hub serves JSON and HTML and
//     nothing else, and `nosniff` means a JSON body cannot be turned into script by
//     a `text/plain`-style guess.
//   - `X-Frame-Options: DENY` stops any page embedding the queue in an iframe. There
//     is no legitimate framing of a single-user tool, and a framed page can be used
//     to make a clickjacking target out of a form that deletes files.
//   - `Content-Security-Policy` with `default-src 'self'` is the one that would
//     actually stop an injected script if an escaping bug appeared. **`'unsafe-inline'`
//     is absent and must stay absent**: the templates put no `<script>` block in a
//     page, so nothing needs it, and adding it back would remove the only protection
//     against a template that starts trusting a filesystem-derived string.
var SecurityHeaders = map[string]string{
	"X-Content-Type-Options": "nosniff",
	"X-Frame-Options":        "DENY",
	"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; " +
		"base-uri 'none'; form-action 'self'",
	// Referrers would otherwise carry a hub URL to whatever the page links to. The
	// queue links to `music.apple.com` in a "source" link, and `/queue` itself is not
	// secret -- but a `Referer` is free to not send.
	"Referrer-Policy": "no-referrer",
}

// Route is one handler, and whether it needs a session.
type route struct {
	method  string
	parts   []string
	guarded bool
	handler func(w http.ResponseWriter, r *http.Request, params map[string]string)
}

// Router is a small path router: literal segments and `{name}` placeholders.
//
// **Hand-written rather than `net/http`'s mux because of `{id}`.** The port targets
// Go 1.20, whose `ServeMux` matches whole patterns and has no wildcards, so
// `/api/jobs/{id}` would be a prefix match over the entire subtree -- including
// `/api/jobs/requeue`, which is a *different route* that must not be read as a job id.
// FastAPI has the same trap and the same answer: literal paths are matched before
// wildcards, because the route table is ordered.
type Router struct {
	routes []route
	state  *State
}

// NewRouter builds every route the hub serves.
func NewRouter(state *State) *Router {
	router := &Router{state: state}

	// The one unguarded route, and the only one a stranger can reach. It answers
	// before anybody has logged in, which is what makes it the healthcheck's target --
	// and therefore the one place a useful-looking extra field would be a disclosure:
	// wrapper state, queue depth and library roots all describe the host, and there is
	// `/api/status` for the authenticated caller who is allowed to see them.
	router.addPlain(http.MethodGet, "/api/health", false, func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
	})

	// The login route is unguarded, and has to be: a route that must be reachable
	// without a session cannot sit behind the guard.
	router.addPlain(http.MethodPost, "/api/auth/login", false, state.handleLogin)
	router.addPlain(http.MethodGet, "/login", false, state.handleLoginPage)
	router.addPlain(http.MethodPost, "/login", false, state.handleLoginForm)
	router.addPlain(http.MethodPost, "/logout", false, state.handleLogoutPage)

	// The static mount. Only the login page's own two files are public, because a
	// login form with no stylesheet and no script is a worse answer than a styled one
	// -- and nothing else is, because "only /api/health is open" has to be true of the
	// whole route table and not just of `/api`.
	router.add(http.MethodGet, "/static/{file}", false, state.handleStatic)

	router.addPlain(http.MethodPost, "/api/auth/logout", true, state.handleLogout)
	router.addPlain(http.MethodGet, "/api/auth/session", true, state.handleSession)

	router.addPlain(http.MethodPost, "/api/wrapper/start", true, state.handleWrapperStart)
	router.addPlain(http.MethodPost, "/api/wrapper/stop", true, state.handleWrapperStop)
	router.addPlain(http.MethodPost, "/api/wrapper/restart", true, state.handleWrapperRestart)
	router.addPlain(http.MethodPost, "/api/wrapper/login", true, state.handleWrapperLogin)
	router.addPlain(http.MethodPost, "/api/wrapper/login/2fa", true, state.handleWrapperTwoFA)

	// Literal paths before `{jobID}`, so `/api/jobs/requeue` is never read as a job.
	router.addPlain(http.MethodGet, "/api/jobs/stream", true, state.handleJobsStream)
	router.addPlain(http.MethodPost, "/api/jobs/requeue", true, state.handleJobsRequeue)
	router.addPlain(http.MethodDelete, "/api/jobs/finished", true, state.handleJobsDeleteFinished)
	router.addPlain(http.MethodPost, "/api/jobs", true, state.handleJobsCreate)
	router.addPlain(http.MethodGet, "/api/jobs", true, state.handleJobsList)
	router.add(http.MethodGet, "/api/jobs/{jobID}", true, state.handleJobGet)
	router.add(http.MethodDelete, "/api/jobs/{jobID}", true, state.handleJobDelete)
	router.add(http.MethodPost, "/api/jobs/{jobID}/retry", true, state.handleJobRetry)

	router.addPlain(http.MethodGet, "/api/library/albums", true, state.handleLibraryAlbums)
	router.addPlain(http.MethodGet, "/api/library/artists", true, state.handleLibraryArtists)
	router.addPlain(http.MethodPost, "/api/library/scan", true, state.handleLibraryScan)
	router.addPlain(http.MethodGet, "/api/library/duplicates", true, state.handleLibraryDuplicates)

	router.addPlain(http.MethodGet, "/api/status", true, state.handleStatus)

	router.addPlain(http.MethodGet, "/", true, state.handleQueuePage)
	router.addPlain(http.MethodGet, "/queue", true, state.handleQueuePage)
	router.addPlain(http.MethodGet, "/library", true, state.handleLibraryPage)
	// The enqueue form's no-JS target. The Python templates post here and no route
	// answered, which made "works without scripting" true of every page but the one
	// the tool is for; this is the route the form always named.
	router.addPlain(http.MethodPost, "/enqueue", true, state.handleEnqueueForm)

	return router
}

// addPlain is `add` for a route with no path parameters, which is most of them.
func (rt *Router) addPlain(method, pattern string, guarded bool,
	handler func(http.ResponseWriter, *http.Request)) {
	rt.add(method, pattern, guarded,
		func(w http.ResponseWriter, r *http.Request, _ map[string]string) { handler(w, r) })
}

func (rt *Router) add(method, pattern string, guarded bool,
	handler func(http.ResponseWriter, *http.Request, map[string]string)) {
	rt.routes = append(rt.routes, route{
		method: method, parts: strings.Split(pattern, "/"), guarded: guarded, handler: handler,
	})
}

// ServeHTTP matches the request, applies the guard, and puts the security headers on
// the response whatever produced it.
//
// A middleware rather than headers on each response class, because the responses that
// matter here are the ones nobody thinks to add headers to: the 401, the 303 from a
// page, the SSE stream, the static files and the 502 from a collaborator.
func (rt *Router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	recorder := &headerRecorder{ResponseWriter: w, header: w.Header()}
	for key, value := range SecurityHeaders {
		recorder.header.Set(key, value)
	}
	rt.serve(recorder, r)
}

func (rt *Router) serve(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimSuffix(r.URL.Path, "/")
	if path == "" {
		path = "/"
	}
	requestParts := strings.Split(path, "/")
	for _, candidate := range rt.routes {
		params, ok := matchRoute(candidate.parts, requestParts)
		if !ok {
			continue
		}
		if candidate.method != r.Method {
			continue
		}
		if candidate.guarded {
			if !rt.state.authenticated(r) {
				// A fetch gets a 401 and a navigation gets the login page: a browser
				// following a redirect to a page is right, and a JSON client that got
				// HTML would have to guess.
				if wantsJSON(r) {
					fail(w, http.StatusUnauthorized, "this route needs a session. "+
						"POST /api/auth/login to get one.")
					return
				}
				http.Redirect(w, r, LoginPath, http.StatusSeeOther)
				return
			}
		}
		candidate.handler(w, r, params)
		return
	}
	// A path that exists under another method is a 405, not a 404: the difference is
	// what tells a client that its URL is right and its verb is not.
	for _, candidate := range rt.routes {
		if _, ok := matchRoute(candidate.parts, requestParts); ok {
			fail(w, http.StatusMethodNotAllowed, fmt.Sprintf(
				"%s is not allowed on %s.", r.Method, r.URL.Path))
			return
		}
	}
	fail(w, http.StatusNotFound, fmt.Sprintf("no route for %s %s.", r.Method, r.URL.Path))
}

// matchRoute matches literal segments and fills in the placeholders.
func matchRoute(pattern, path []string) (map[string]string, bool) {
	if len(pattern) != len(path) {
		return nil, false
	}
	params := map[string]string{}
	for index, segment := range pattern {
		if strings.HasPrefix(segment, "{") && strings.HasSuffix(segment, "}") {
			params[strings.Trim(segment, "{}")] = path[index]
			continue
		}
		if segment != path[index] {
			return nil, false
		}
	}
	return params, true
}

// headerRecorder exists so that a handler that writes a status before the middleware
// can still be given the headers: `Header()` is the same map, and nothing else is
// intercepted.
type headerRecorder struct {
	http.ResponseWriter
	header http.Header
}

func (h *headerRecorder) Header() http.Header { return h.header }

// Flush passes through, and its absence was a real bug: without it the SSE handler's
// type assertion fails, and the hub answers "this server cannot stream" to the one
// route whose whole purpose is streaming.
func (h *headerRecorder) Flush() {
	if flusher, ok := h.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

// --------------------------------------------------------------------------- #
// responses
// --------------------------------------------------------------------------- #

// LoginPath is the page every unauthenticated navigation is sent to.
const LoginPath = "/login"

// PublicStatic is the whole exception list for `/static`, and it is two files: the
// login page has to be able to render without a session, and a login form with no
// stylesheet and no script is a worse answer than a styled one.
var PublicStatic = map[string]bool{
	"/static/app.css": true,
	"/static/app.js":  true,
	// The favicon is fetched by the browser before anyone has logged in -- on the login
	// page itself, which is the one page a stranger can reach. A guarded favicon is a 401
	// in the devtools console on every visit and a blank tab icon for ever.
	"/static/favicon.svg": true,
}

// writeJSON writes one JSON body.
func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(body)
}

// fail writes the error shape every JSON route uses: a `detail`, plus whatever the
// caller adds.
func fail(w http.ResponseWriter, status int, detail string, extra ...map[string]any) {
	body := map[string]any{"detail": detail}
	for _, values := range extra {
		for key, value := range values {
			body[key] = value
		}
	}
	writeJSON(w, status, body)
}

// wantsJSON reports whether the caller is a fetch rather than a navigation.
//
// The distinction decides between a 401 and a redirect, and it is made on the headers
// the browser sets and a fetch sets: `Accept` and `X-Requested-With`. A client that
// asks for anything and gets a login page would have to guess why.
func wantsJSON(r *http.Request) bool {
	if strings.Contains(r.URL.Path, "/api/") {
		return true
	}
	if strings.EqualFold(r.Header.Get("X-Requested-With"), "XMLHttpRequest") {
		return true
	}
	return strings.Contains(r.Header.Get("Accept"), "application/json")
}

// decodeBody reads a JSON body, tolerating an absent one.
//
// An absent or unparseable body is not a 422 on any route here: the login route has to
// answer "wrong password" for `{"password": 5}` and for a body that is not JSON at all,
// and the others have defaults for every field.
func decodeBody(r *http.Request, out any) {
	if r.Body == nil {
		return
	}
	decoder := json.NewDecoder(r.Body)
	_ = decoder.Decode(out)
}

// readForm parses a form body, which is what the no-JS paths use.
func readForm(r *http.Request) map[string]string {
	_ = r.ParseForm()
	values := map[string]string{}
	for key, list := range r.PostForm {
		if len(list) > 0 {
			values[key] = list[0]
		}
	}
	return values
}

// authenticated is the session check every guarded route and the static mount share.
func (s *State) authenticated(r *http.Request) bool {
	return s.Sessions.FromRequest(r, time.Now(), s.SessionGeneration()) == nil
}

// --------------------------------------------------------------------------- #
// errors from collaborators
// --------------------------------------------------------------------------- #

// statusForError maps a collaborator's error onto a status, the way the Python
// exception handlers did.
//
// The three named types are the ones a *user* can cause or fix -- an unexpandable URL,
// a wrapper that will not start, a client that is not running -- and they are 502 or
// 503 rather than 500, because "the thing behind the hub refused" is not "the hub is
// broken". Anything else is a bug, and a bug is a 500 with the message and no
// pretence of being actionable.
func statusForError(err error) (int, string) {
	var clientError *clientFailure
	if errors.As(err, &clientError) {
		return clientError.status, clientError.detail
	}
	var storeError *jobs.StoreError
	if errors.As(err, &storeError) {
		return http.StatusInternalServerError, err.Error()
	}
	var notFound *jobs.NotFoundError
	if errors.As(err, &notFound) {
		return http.StatusNotFound, err.Error()
	}
	return http.StatusInternalServerError, err.Error()
}

// clientFailure is an error from the wrapper or the client, with the status the API
// layer should answer.
type clientFailure struct {
	status int
	detail string
}

func (e *clientFailure) Error() string { return e.detail }

// sortedKeys is used wherever a map's keys go into a message, so the message and the
// map cannot disagree about order.
func sortedKeys(values map[string]bool) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
