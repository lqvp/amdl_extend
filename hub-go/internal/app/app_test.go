package app

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"amdhub/internal/config"
	"amdhub/internal/events"
	"amdhub/internal/jobs"
)

// testState builds a hub with a temporary database and library root, and a wrapper whose
// binary does not exist.
//
// **Nothing here touches the network or spawns a process**, and that is deliberate: the
// tests below are about the HTTP contract -- what is guarded, what is a 401 and what is a
// 303, which keys the JSON has -- and every one of those is answerable without an Apple
// account. The worker is left unstarted, which is also the state a fresh image is in
// before the operator logs in, and the one the 503 below is about.
func testState(t *testing.T) *State {
	t.Helper()
	t.Setenv("AMD_PASSWORD", "hunter2")
	t.Setenv("AMD_LIBRARY_ROOTS", t.TempDir())
	t.Setenv("AMD_DB_PATH", t.TempDir()+"/hub.db")
	t.Setenv("AMD_WRAPPER_BINARY", t.TempDir()+"/no-such-wrapper")
	settings, err := config.Load(nil)
	if err != nil {
		t.Fatalf("config.Load: %v", err)
	}
	state, err := New(settings)
	if err != nil {
		t.Fatalf("app.New: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		state.Close(ctx)
	})
	return state
}

func testServer(t *testing.T) (*State, *httptest.Server) {
	t.Helper()
	state := testState(t)
	server := httptest.NewServer(NewRouter(state))
	t.Cleanup(server.Close)
	return state, server
}

// login is the JSON login, and returns the session cookie it was given.
func login(t *testing.T, server *httptest.Server, password string) (*http.Response, []*http.Cookie) {
	t.Helper()
	resp, err := http.Post(server.URL+"/api/auth/login", "application/json",
		strings.NewReader(`{"password":"`+password+`"}`))
	if err != nil {
		t.Fatalf("login: %v", err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp, resp.Cookies()
}

func get(t *testing.T, server *httptest.Server, path string, cookies []*http.Cookie) (*http.Response, string) {
	t.Helper()
	request, err := http.NewRequest(http.MethodGet, server.URL+path, nil)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	for _, cookie := range cookies {
		request.AddCookie(cookie)
	}
	// No redirect following: the 303 to /login is the answer being asserted, and a client
	// that followed it would report the login page's 200 instead.
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	resp, err := client.Do(request)
	if err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close() //nolint:errcheck // read below, closed after
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("GET %s: read: %v", path, err)
	}
	return resp, string(body)
}

// --------------------------------------------------------------------------- //
// The guard
// --------------------------------------------------------------------------- //
func TestHealthIsTheOneUnguardedRouteAndCarriesTheSecurityHeaders(t *testing.T) {
	_, server := testServer(t)
	resp, body := get(t, server, "/api/health", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health: %d %s", resp.StatusCode, body)
	}
	if strings.TrimSpace(body) != `{"status":"ok"}` {
		t.Fatalf("health body: %q", body)
	}
	for header, want := range map[string]string{
		"X-Content-Type-Options":  "nosniff",
		"X-Frame-Options":         "DENY",
		"Referrer-Policy":         "no-referrer",
		"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
	} {
		if got := resp.Header.Get(header); got != want {
			t.Errorf("%s = %q, want %q", header, got, want)
		}
	}
}

func TestAnAPIWithoutASessionIsJSON401WhileAPageRedirects(t *testing.T) {
	_, server := testServer(t)

	resp, body := get(t, server, "/api/status", nil)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("api status without a session: %d %s", resp.StatusCode, body)
	}
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "application/json") {
		t.Errorf("an API 401 has to be JSON for `app.js` to read it: %q",
			resp.Header.Get("Content-Type"))
	}

	resp, body = get(t, server, "/queue", nil)
	if resp.StatusCode != http.StatusSeeOther {
		t.Fatalf("queue page without a session: %d %s", resp.StatusCode, body)
	}
	if location := resp.Header.Get("Location"); location != "/login" {
		t.Errorf("a browser has to be sent to the login page, got %q", location)
	}

	// The login page itself is unguarded -- it is where the redirect points.
	resp, _ = get(t, server, "/login", nil)
	if resp.StatusCode != http.StatusOK {
		t.Errorf("the login page must be reachable without a session: %d", resp.StatusCode)
	}
}

func TestTheStaticAssetsArePublicAndTheOthersAreNot(t *testing.T) {
	_, server := testServer(t)
	for _, path := range []string{"/static/app.css", "/static/app.js"} {
		resp, _ := get(t, server, path, nil)
		if resp.StatusCode != http.StatusOK {
			t.Errorf("%s has to be served without a session (the login page loads it): %d",
				path, resp.StatusCode)
		}
	}
	// Anything not in the embedded pair is refused rather than looked for on disk: the
	// binary has no static directory to fall back to. Without a session it is a 401 --
	// the mount is guarded like everything else, and only the two names above are public.
	if resp, _ := get(t, server, "/static/app.js.map", nil); resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("an asset the binary does not embed must need a session, got %d", resp.StatusCode)
	}
	_, cookies := login(t, server, "hunter2")
	if resp, _ := get(t, server, "/static/app.js.map", cookies); resp.StatusCode != http.StatusNotFound {
		t.Errorf("with a session it must be 404, got %d", resp.StatusCode)
	}
}

// --------------------------------------------------------------------------- //
// Sessions
// --------------------------------------------------------------------------- //
func TestLoginGrantsASessionAndLogoutRevokesTheGeneration(t *testing.T) {
	state, server := testServer(t)

	resp, cookies := login(t, server, "hunter2")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login: %d", resp.StatusCode)
	}
	if len(cookies) == 0 {
		t.Fatal("login returned no cookie")
	}
	before := state.SessionGeneration()

	resp, body := get(t, server, "/api/status", cookies)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status with a session: %d %s", resp.StatusCode, body)
	}
	var status map[string]any
	if err := json.Unmarshal([]byte(body), &status); err != nil {
		t.Fatalf("status is not JSON: %v (%s)", err, body)
	}
	for _, key := range []string{"wrapper", "library", "queue", "dedup_artist_scope"} {
		if _, ok := status[key]; !ok {
			t.Errorf("/api/status has no %q key: %s", key, body)
		}
	}

	// The wrong password is a 401 and grants nothing.
	if resp, _ := login(t, server, "wrong"); resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("a wrong password must be 401, got %d", resp.StatusCode)
	}

	if resp, body := get(t, server, "/api/auth/logout", cookies); resp.StatusCode == http.StatusOK {
		_ = body // GET is not the logout method; the POST below is.
	}
	request, _ := http.NewRequest(http.MethodPost, server.URL+"/api/auth/logout", nil)
	for _, cookie := range cookies {
		request.AddCookie(cookie)
	}
	if resp, err := http.DefaultClient.Do(request); err != nil {
		t.Fatalf("logout: %v", err)
	} else {
		resp.Body.Close() //nolint:errcheck // nothing to read
	}

	if state.SessionGeneration() != before+1 {
		t.Errorf("logout has to retire the generation: %d -> %d", before, state.SessionGeneration())
	}
	if resp, _ := get(t, server, "/api/status", cookies); resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("a revoked cookie has to stop working, got %d", resp.StatusCode)
	}
}

// --------------------------------------------------------------------------- //
// The job contract
// --------------------------------------------------------------------------- //
func TestJobJSONIsTheDocumentedShapeWithNoFabricatedMusicVideoKey(t *testing.T) {
	state, server := testServer(t)
	_, cookies := login(t, server, "hunter2")

	if _, err := state.Store.CreateBatch("https://music.apple.com/jp/album/1", "album",
		[]jobs.Leaf{{
			AdamID: "1440833098", Title: "A Track", AlbumName: "An Album",
			ArtistName: "An Artist", Codec: "alac", Language: "jp",
			URL: "https://music.apple.com/jp/song/1", Storefront: "jp",
		}}, false); err != nil {
		t.Fatalf("CreateBatch: %v", err)
	}

	resp, body := get(t, server, "/api/jobs", cookies)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("jobs: %d %s", resp.StatusCode, body)
	}
	var payload struct {
		Jobs []map[string]any `json:"jobs"`
	}
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		t.Fatalf("jobs is not JSON: %v (%s)", err, body)
	}
	if len(payload.Jobs) != 1 {
		t.Fatalf("expected the one queued job, got %d: %s", len(payload.Jobs), body)
	}
	job := payload.Jobs[0]
	for _, key := range []string{
		"id", "adam_id", "title", "status", "codec", "language", "parent_url",
		"parent_type", "parent_id", "force", "progress", "bytes_done", "bytes_total",
		"skip_reason", "error", "created_at", "started_at", "finished_at",
	} {
		if _, ok := job[key]; !ok {
			t.Errorf("the job contract has no %q key: %s", key, body)
		}
	}
	// **The absence is the assertion.** A music-video job would be reported as `false` by a
	// fabricated key, and the `job` table has no column to answer with -- the leaf registry
	// does. `test_the_job_contract_does_not_fabricate_is_music_video` holds the Python side
	// of the same line.
	if _, fabricated := job["is_music_video"]; fabricated {
		t.Error("job JSON must not carry an `is_music_video` key: it is not a column")
	}
	if status, _ := job["status"].(string); status != "queued" {
		t.Errorf("a freshly queued job reads %q, want queued", job["status"])
	}
}

func TestEnqueueWithoutAClientIs503RatherThanAnEmptyQueue(t *testing.T) {
	_, server := testServer(t)
	_, cookies := login(t, server, "hunter2")

	request, _ := http.NewRequest(http.MethodPost, server.URL+"/api/jobs",
		strings.NewReader(`{"urls":["https://music.apple.com/jp/album/1"],"codec":"alac"}`))
	request.Header.Set("Content-Type", "application/json")
	for _, cookie := range cookies {
		request.AddCookie(cookie)
	}
	resp, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck // read below
	body, _ := io.ReadAll(resp.Body)
	// 503, not 400: the request was well formed and the hub is the thing that cannot
	// serve it. The detail has to name the client, or the operator reads it as a bad URL.
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("enqueue without a client: %d %s", resp.StatusCode, body)
	}
	if !strings.Contains(strings.ToLower(string(body)), "client") {
		t.Errorf("the 503 has to say the client is the problem: %s", body)
	}
}

func TestAMissingJobIs404AndNotAClosedConnection(t *testing.T) {
	_, server := testServer(t)
	_, cookies := login(t, server, "hunter2")

	// Every route that names a job id, because they each read the row for a different
	// reason -- `get` to render it, `delete` to refuse a running one, `retry` to refuse a
	// queued one -- and a missing row reaches the same nil.
	for _, target := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/api/jobs/999"},
		{http.MethodDelete, "/api/jobs/999"},
		{http.MethodPost, "/api/jobs/999/retry"},
	} {
		request, err := http.NewRequest(target.method, server.URL+target.path, nil)
		if err != nil {
			t.Fatalf("request: %v", err)
		}
		for _, cookie := range cookies {
			request.AddCookie(cookie)
		}
		resp, err := http.DefaultClient.Do(request)
		if err != nil {
			// A panic in the handler closes the connection, and that is the failure this
			// test was written for: `Store.Get` reports a missing row as `(nil, nil)`.
			t.Fatalf("%s %s: %v", target.method, target.path, err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close() //nolint:errcheck // read above
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("%s %s: %d, want 404 (%s)", target.method, target.path,
				resp.StatusCode, body)
			continue
		}
		if !strings.Contains(string(body), "999") {
			t.Errorf("%s %s: the 404 has to name the id: %s", target.method, target.path, body)
		}
	}
}

// --------------------------------------------------------------------------- //
// The stream
// --------------------------------------------------------------------------- //
func TestTheStreamOpensWithTheRetryFieldAndThenTheSnapshot(t *testing.T) {
	state, server := testServer(t)
	_, cookies := login(t, server, "hunter2")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		server.URL+"/api/jobs/stream", nil)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	for _, cookie := range cookies {
		request.AddCookie(cookie)
	}
	resp, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck // cancelled below
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stream: %d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("stream content type: %q", ct)
	}
	if buffering := resp.Header.Get("X-Accel-Buffering"); buffering != "no" {
		t.Errorf("a proxied stream has to say `no` to buffering, got %q", buffering)
	}

	// The whole opening frame, exactly: `retry: 2000` and the blank line that ends it. A
	// client that reconnects into the browser's default three seconds re-takes the
	// snapshot, and the field is the only way the hub gets to say otherwise.
	head := make([]byte, len("retry: 2000\n\n"))
	if _, err := io.ReadFull(resp.Body, head); err != nil {
		t.Fatalf("reading the retry frame: %v", err)
	}
	if string(head) != "retry: 2000\n\n" {
		t.Fatalf("the stream must open with `retry: 2000`, got %q", head)
	}

	// Then the snapshot, as the first `data:` frame -- never a stale backlog frame and
	// never nothing at all.
	reader := newFrameReader(resp.Body)
	frame, err := reader.next(t, 5*time.Second)
	if err != nil {
		t.Fatalf("reading the first frame: %v", err)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(frame), &message); err != nil {
		t.Fatalf("the first frame is not JSON: %v (%q)", err, frame)
	}
	if kind, _ := message["kind"].(string); kind != "snapshot" {
		t.Fatalf("the first frame after `retry` must be the snapshot, got %q", kind)
	}
	if _, ok := message["jobs"]; !ok {
		t.Fatalf("the snapshot has to carry a jobs list: %q", frame)
	}

	// And when the client goes away the subscriber goes with it. This is the leak the
	// Python stream guards with `finally: aclose()`: a tab that closes has to stop being
	// a subscriber, or every tab ever opened stays one for the process's life.
	cancel()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if state.Broker.SubscriberCount(events.JobsChannel) == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Errorf("the subscriber was never removed after the client disconnected: %d left",
		state.Broker.SubscriberCount(events.JobsChannel))
}

// frameReader reads `data:` lines off an SSE body, one frame at a time.
//
// A tiny reader rather than an `EventSource` client because the assertion is about the
// *bytes*: the first frame has to be the snapshot, and a client library that buffered
// would hide the difference between "first" and "eventually".
type frameReader struct {
	reader io.Reader
	buffer []byte
}

func newFrameReader(reader io.Reader) *frameReader {
	return &frameReader{reader: reader}
}

func (f *frameReader) next(t *testing.T, timeout time.Duration) (string, error) {
	t.Helper()
	type result struct {
		line string
		err  error
	}
	done := make(chan result, 1)
	go func() {
		for {
			if index := strings.Index(string(f.buffer), "\n"); index >= 0 &&
				strings.HasPrefix(string(f.buffer), "data: ") {
				line := string(f.buffer)[:index]
				f.buffer = f.buffer[index+1:]
				done <- result{line: strings.TrimPrefix(line, "data: ")}
				return
			}
			chunk := make([]byte, 512)
			count, err := f.reader.Read(chunk)
			if count > 0 {
				f.buffer = append(f.buffer, chunk[:count]...)
				continue
			}
			if err != nil {
				done <- result{err: err}
				return
			}
		}
	}()
	select {
	case found := <-done:
		return found.line, found.err
	case <-time.After(timeout):
		return "", context.DeadlineExceeded
	}
}
