// Package app is the port of `hub/app.py` plus `hub/api/`: the process's state, the
// scheduler, and the HTTP surface.
//
// **One package, and that is the shape of the original rather than a shortcut.** The
// Python split `app.py` from `api/` because FastAPI's router objects and Jinja
// templates made that natural, but everything both halves touch lives on
// `app.state` -- the broker, the store, the leaf registry, the scheduler, the session
// generation -- and a Go port that put the handlers in their own package would need
// an interface naming every one of those fields, or a function per handler, to say
// nothing the same file cannot. The files are split the way the Python modules are
// (`http_jobs.go`, `http_wrapper.go`, ...); the package is not.
package app

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strings"
	"sync"
	"time"

	"amdhub/internal/auth"
	"amdhub/internal/config"
	"amdhub/internal/dedup"
	"amdhub/internal/events"
	"amdhub/internal/jobs"
	"amdhub/internal/library"
	"amdhub/internal/normalize"
	"amdhub/internal/pyworker"
	"amdhub/internal/wrapper"
)

// The scheduler's timing. Every one is the Python module's, and every one has a
// reason: see `scheduler.go`.
const (
	// IdlePollSeconds is the queue poll when nothing is queued -- a SQLite read, not
	// an HTTP request.
	IdlePollSeconds = 500 * time.Millisecond
	// IdleReadinessPollSeconds is the poll while the wrapper cannot serve: the thing
	// being waited on is the wrapper coming back, and there is nothing to gain from
	// looking for new work twice a second.
	IdleReadinessPollSeconds = 1 * time.Second
	// PostJobPollSeconds is the poll right after a job ran, when the wrapper was
	// ready moments ago.
	PostJobPollSeconds = 500 * time.Millisecond
	// DrainTimeout is how long a shutdown waits for an in-flight rip.
	DrainTimeout = 300 * time.Second
)

// State is everything the process owns. One per process -- see `main`, and the
// `workers=1` argument in the image's entry point.
type State struct {
	Settings   *config.Settings
	Store      *jobs.Store
	Broker     *events.Broker
	Sessions   *auth.Sessions
	Supervisor *wrapper.Supervisor
	Worker     *pyworker.Worker
	Leaves     *LeafRegistry

	// Log is where the wrapper supervisor's lines go: onto the stream, onto stderr,
	// and into the ring the queue page renders before the first frame arrives.
	Log *LogRing

	mu                sync.Mutex
	sessionGeneration int
	startupError      string
	currentJob        *int64
	pendingChallenge  *wrapper.Challenge
	degradedRoots     string
	cachedProblem     string
	stopping          bool
	stopOnce          sync.Once
	stopped           chan struct{}
}

// New builds the state. `autostart` is deliberately not a parameter: `Start` is what
// the caller does, so that a test can build a state without a wrapper.
func New(settings *config.Settings) (*State, error) {
	store, err := jobs.Open(settings.DBPath)
	if err != nil {
		return nil, err
	}
	state := &State{
		Settings: settings,
		Store:    store,
		Broker:   events.New(),
		Sessions: auth.NewSessions(settings.SessionSecret),
		Leaves:   NewLeafRegistry(4096),
		Log:      NewLogRing(200),
		stopped:  make(chan struct{}),
	}
	state.Supervisor = wrapper.New(wrapper.Config{
		Binary:        settings.WrapperBinary,
		BaseDir:       settings.WrapperBaseDir,
		Host:          settings.WrapperHost,
		Port:          settings.WrapperPort,
		LogSink:       state.logLine,
		AdoptExisting: true,
	})
	return state, nil
}

// logLine is the supervisor's log sink, and the port of `app._log`.
//
// **The supervisor scrubs this before it gets here** (R6: credentials are replaced
// before the line leaves the child's pipe), which is why the hub can display a
// child's log lines at all. It is published on the jobs channel so one SSE stream
// carries the queue and the log together, in the order they happened.
func (s *State) logLine(line string) {
	s.Log.Add(line)
	s.Broker.Publish(events.JobsChannel, events.Message{"kind": "log", "line": line})
	log.Print(line)
}

// StartWorker launches the Python client if the image has one, and reports what
// happened without failing the boot.
//
// The hub boots whether or not the client can start: a missing account, a missing
// vendor tree or a missing dependency is a deployment problem the *UI* reports, and
// a container that refuses to start cannot report anything. The worker failing is
// therefore a log line and a nil worker, and every op that needs one answers with
// the client's own error text.
func (s *State) StartWorker(ctx context.Context) error {
	if s.Settings.PythonWorker == "" {
		s.logLine("no AMD_PYTHON_WORKER is configured, so the Apple client is not " +
			"available: URLs cannot be expanded and downloads cannot run")
		return nil
	}
	worker, err := pyworker.Start(pyworker.Options{
		Python: s.Settings.Python,
		Script: s.Settings.PythonWorker,
		Config: s.Settings.VendorConfig,
		Dir:    s.Settings.WorkerDir,
		Env:    s.Settings.WorkerEnv,
	})
	if err != nil {
		s.logLine(fmt.Sprintf("the Apple client did not start: %v", err))
		return nil
	}
	worker.SetProgressHandler(s.applyProgress)
	worker.SetLogHandler(s.logLine)
	s.mu.Lock()
	s.Worker = worker
	s.mu.Unlock()
	s.logLine("the Apple client worker is running")
	go func() {
		<-worker.Done()
		if err := worker.Err(); err != nil {
			s.logLine(fmt.Sprintf("the Apple client stopped: %v", err))
		}
	}()
	return nil
}

// Worker returns the running client, or an error that says why it is not available.
func (s *State) WorkerHandle() (*pyworker.Worker, error) {
	s.mu.Lock()
	worker := s.Worker
	s.mu.Unlock()
	if worker == nil {
		return nil, fmt.Errorf("the downloader client is not started, so there is nothing " +
			"to expand a URL against. This is a startup failure, not a bad request; the log " +
			"says why.")
	}
	return worker, nil
}

// Close shuts everything down, in the order that matters.
func (s *State) Close(ctx context.Context) {
	s.Stop()
	if worker, err := s.WorkerHandle(); err == nil {
		// `shutdown` lets `RipperHost.close()` release the client's own threads and
		// temp files; the kill inside `Close` is only for a worker that did not take
		// the hint.
		_ = worker.Close(DrainTimeout)
	}
	s.Supervisor.Stop()
	_ = s.Store.Close()
}

// Stop signals the scheduler to stop and waits for a drain.
func (s *State) Stop() {
	s.stopOnce.Do(func() {
		s.mu.Lock()
		s.stopping = true
		s.mu.Unlock()
		close(s.stopped)
	})
}

// Stopped is closed when the process has been asked to stop.
func (s *State) Stopped() <-chan struct{} { return s.stopped }

// Stopping reports whether a shutdown is in progress.
func (s *State) Stopping() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stopping
}

// SessionGeneration is the current generation, which is what logout bumps.
func (s *State) SessionGeneration() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sessionGeneration
}

// RetireSession bumps the generation, invalidating every token ever issued.
func (s *State) RetireSession() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sessionGeneration = auth.Retire(s.sessionGeneration)
	return s.sessionGeneration
}

// StartupError is the message left by a wrapper that would not start, kept so that a
// later `/api/status` can still explain a wrapper that is not running.
func (s *State) StartupError() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.startupError
}

// SetStartupError records, or clears, that message.
func (s *State) SetStartupError(message string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.startupError = message
}

// CachedProblem is the last readiness verdict the scheduler observed, for `/api/status`.
func (s *State) CachedProblem() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cachedProblem
}

// --------------------------------------------------------------------------- #
// the leaf registry
// --------------------------------------------------------------------------- #

// LeafRegistry is `job id -> Leaf`, for the leaves this process expanded.
//
// **Why it has to exist.** The `job` table stores `adam_id`, `title`, `codec` and
// `language` -- deliberately, because the filesystem is the source of truth for what
// is downloaded and a description of the track is operational state. But the
// consumers of a job need more: `run_song` needs the storefront, and the duplicate
// check needs the album name and the artist. So the expansion is held here, from the
// request that made it until the job finishes.
//
// **What a miss means, and why it is a failure rather than a blank.** A row whose
// leaf this process never saw was enqueued by a previous hub process. `leafFor`
// re-expands the parent URL in that case, which is why a restart is not a reason to
// lose a queue -- and when even that cannot produce the track, the job is failed with
// a message naming the id. Running it with blanks instead would be worse than
// useless: a blank album name normalizes to "", and `find_duplicate` refuses an empty
// key, so the check would silently pass every time.
//
// Bounded, because a map keyed by job id that only shrinks when the scheduler runs
// would grow for the life of the process. Oldest ids go first, which is the order the
// queue would run them in anyway.
type LeafRegistry struct {
	mu       sync.Mutex
	capacity int
	leaves   map[int64]jobs.Leaf
	order    []int64
}

// NewLeafRegistry returns a registry holding at most `capacity` leaves.
func NewLeafRegistry(capacity int) *LeafRegistry {
	return &LeafRegistry{capacity: capacity, leaves: map[int64]jobs.Leaf{}}
}

// Get returns the leaf for a job, if this process expanded one.
func (r *LeafRegistry) Get(jobID int64) (jobs.Leaf, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	leaf, ok := r.leaves[jobID]
	return leaf, ok
}

// Put remembers a leaf, evicting the oldest if the registry is full.
func (r *LeafRegistry) Put(jobID int64, leaf jobs.Leaf) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.leaves[jobID]; !exists {
		r.order = append(r.order, jobID)
	}
	r.leaves[jobID] = leaf
	for len(r.order) > r.capacity {
		oldest := r.order[0]
		r.order = r.order[1:]
		delete(r.leaves, oldest)
	}
}

// Forget drops a job's leaf. Called wherever a job stops being queued.
func (r *LeafRegistry) Forget(jobID int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.leaves[jobID]; !exists {
		return
	}
	delete(r.leaves, jobID)
	for i, id := range r.order {
		if id == jobID {
			r.order = append(r.order[:i], r.order[i+1:]...)
			break
		}
	}
}

// Len is how many leaves are held, which is what a leak test asserts.
func (r *LeafRegistry) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.leaves)
}

// --------------------------------------------------------------------------- #
// the log ring
// --------------------------------------------------------------------------- #

// LogRing keeps the last few log lines, so the queue page renders a log pane that
// has content before the first SSE frame arrives.
type LogRing struct {
	mu    sync.Mutex
	limit int
	lines []string
}

// NewLogRing returns a ring holding `limit` lines.
func NewLogRing(limit int) *LogRing {
	return &LogRing{limit: limit}
}

// Add appends one line, dropping the oldest past the limit.
func (r *LogRing) Add(line string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lines = append(r.lines, line)
	if len(r.lines) > r.limit {
		r.lines = r.lines[len(r.lines)-r.limit:]
	}
}

// Lines is a copy of the ring, oldest first.
func (r *LogRing) Lines() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.lines...)
}

// --------------------------------------------------------------------------- #
// reads the API layer needs
// --------------------------------------------------------------------------- #

// LibrarySummary is the library's reachability, from a real walk and nothing cached.
//
// A stale "1 album found" on an unmounted drive is worse than a slow page, and the
// walk is measured in tens of milliseconds.
//
// `PerRoot` is positional with `Roots`, and it is here rather than only on the
// library page because `/api/status` is what an operator reads: a drive that is not
// plugged in can be mounted-and-empty rather than missing, and then `DegradedRoots`
// is empty and nothing says so. `Albums` alone cannot distinguish that from a full
// library; the per-root count can.
type LibrarySummary struct {
	Roots         []string `json:"roots"`
	DegradedRoots []string `json:"degraded_roots"`
	PerRoot       []int    `json:"per_root"`
	Albums        int      `json:"albums"`
}

// LibrarySummary walks every root and summarises the result.
func (s *State) LibrarySummary() LibrarySummary {
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	summary := LibrarySummary{
		Roots:         append([]string(nil), scan.Roots...),
		DegradedRoots: scan.Degraded(),
		PerRoot:       scan.PerRoot(),
		Albums:        len(scan.Albums),
	}
	if summary.DegradedRoots == nil {
		summary.DegradedRoots = []string{}
	}
	if summary.Roots == nil {
		summary.Roots = []string{}
	}
	return summary
}

// warnDegraded says so, once per change, when a configured root cannot be read.
//
// An unmounted external drive must be **loud**: loose dedup against the surviving
// roots still works, so the failure mode without this is a quiet re-download of
// everything that lived on the missing drive -- and the operator has no way to tell
// that from the hub being broken.
func (s *State) warnDegraded(scan *library.Scan) {
	current := strings.Join(scan.Degraded(), "\x00")
	s.mu.Lock()
	changed := current != s.degradedRoots
	s.degradedRoots = current
	s.mu.Unlock()
	if !changed {
		return
	}
	degraded := scan.Degraded()
	if len(degraded) == 0 {
		s.Broker.Publish(events.JobsChannel, events.Message{
			"kind": "library", "degraded_roots": []string{}, "detail": "",
		})
		return
	}
	s.Broker.Publish(events.JobsChannel, events.Message{
		"kind":           "library",
		"degraded_roots": degraded,
		"detail": fmt.Sprintf("these library roots could not be read: %s. Downloads "+
			"continue and duplicates are still detected against the roots that are there, "+
			"but a track that lived on a missing drive will be downloaded again. Check that "+
			"the drive is mounted.", strings.Join(degraded, ", ")),
	})
}

// Counts is `_jobs_counts`: how many jobs are in each state, from one table read.
//
// `total` is the sum rather than a separate count, so it cannot disagree with the
// parts if a status is ever added to the store's vocabulary without being added here.
func (s *State) Counts() (map[string]int, error) {
	all, err := s.Store.List(jobs.ListFilter{})
	if err != nil {
		return nil, err
	}
	counts := map[string]int{}
	for _, job := range all {
		counts[job.Status]++
	}
	counts["total"] = len(all)
	return counts, nil
}

// JobDict is a `Job` as JSON: every field, and nothing added.
//
// **No `is_music_video` key.** It was there in Python as a hardcoded `False`, which
// is the one value a music-video job must never report: the `job` table has no column
// for it, so the key was a fabrication in the public contract. The value is available
// to a caller that needs it -- the leaf registry holds the leaf for every job this
// process queued -- and round-tripping it through a leaf that may be absent is a
// worse answer than its absence.
func JobDict(job *jobs.Job) map[string]any {
	// The wire names are the Python dataclass's, because the browser's `app.js` reads
	// them: `parent_url`/`parent_type` for the `url`/`url_type` columns, and progress
	// as a fraction or null. Written out rather than reflected over, so a column added
	// to the table is a visible omission here instead of a field that appears in the
	// JSON under a name the client does not expect.
	return map[string]any{
		"id":          job.ID,
		"adam_id":     job.AdamID,
		"title":       job.Title,
		"status":      job.Status,
		"codec":       job.Codec,
		"language":    job.Language,
		"parent_url":  job.ParentURL,
		"parent_type": job.ParentType,
		"parent_id":   job.ParentID,
		"force":       job.Force,
		"progress":    job.Progress,
		"bytes_done":  job.BytesDone,
		"bytes_total": job.BytesTotal,
		"skip_reason": job.SkipReason,
		"error":       job.Error,
		"created_at":  job.CreatedAt,
		"started_at":  job.StartedAt,
		"finished_at": job.FinishedAt,
	}
}

// RequeueScopes is the API's scope names, mapped to the statuses they move.
//
// `unfinished` deliberately includes `skipped`, which on a dedup-heavy library is the
// largest group and re-skips the moment it runs -- the scope is a choice rather than a
// constant because the user makes it.
var RequeueScopes = map[string][]string{
	"failed": {"failed"},
	// "everything except done" is the concept a user has; the store's own exclusion of
	// `running` and `queued` is what makes the set legal. A caller that could pass
	// statuses directly would eventually pass `running`.
	"unfinished": {"queued", "waiting", "failed", "skipped", "cancelled"},
}

// RequeueScopeNames is the sorted list for an error message, so the message and the
// map cannot disagree.
func RequeueScopeNames() []string {
	names := make([]string, 0, len(RequeueScopes))
	for name := range RequeueScopes {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// Codecs is the set of codecs this client can rip.
//
// Checked at the API edge because a value outside the set would be stored in a
// NOT NULL column and fail once, inside the client's retry loop, long after the
// request that carried it.
var Codecs = map[string]bool{
	"alac": true, "ec3": true, "ac3": true, "aac-binaural": true,
	"aac-downmix": true, "aac": true, "aac-legacy": true,
}

// ParentTypes is the `url_type` column's five values, checked in `createBatch`.
var ParentTypes = map[string]bool{
	"song": true, "album": true, "artist": true, "playlist": true, "music-video": true,
}

// ParentTypeFor infers the `url_type` for a parent URL from its shape.
//
// The URL is the only thing that knows: `expand` returns leaves and not a kind, and a
// share link carries the same path segments as the page it came from.
func ParentTypeFor(url string, leafCount int) string {
	if strings.Contains(url, "/music-video/") {
		return "music-video"
	}
	segment := pathSegment(url)
	switch segment {
	case "song":
		return "song"
	case "album":
		return "album"
	case "artist":
		return "artist"
	case "playlist":
		return "playlist"
	}
	// A URL whose shape says nothing: an explicit `?i=` names one track, and anything
	// else that expanded to a single track is a song. See `_path_segment` in the
	// Python original, which this mirrors.
	if strings.Contains(url, "?i=") || leafCount == 1 {
		return "song"
	}
	return "album"
}

// pathSegment is the segment before the identifier in a `music.apple.com` URL.
func pathSegment(url string) string {
	trimmed := url
	if index := strings.Index(trimmed, "//"); index >= 0 {
		trimmed = trimmed[index+2:]
	}
	parts := strings.Split(trimmed, "/")
	for index, part := range parts {
		switch part {
		case "song", "album", "artist", "playlist", "music-video":
			if index+1 < len(parts) && parts[index+1] != "" {
				return part
			}
		}
	}
	return ""
}

// SkipReason is `duplicate:<path>|<path>`, in the order `FindDuplicate` sorted.
//
// **`hit.Resolved`, not `hit.Matched`, and the difference is the whole point.**
// `Matched` holds bare relpaths and the dedup pools candidates across every
// configured root, so with two roots those strings name directories that exist under
// *neither* -- the user is told where a track already is and cannot go and look.
// `Resolved` carries the same evidence as `roots[root_index] / relpath`, which opens,
// and which stays distinct when one relpath appears under two roots.
func SkipReason(hit *dedup.Hit) string {
	return "duplicate:" + strings.Join(hit.Resolved, "|")
}

// Normalize is exported for the tests that pin the dedup key against the Python one.
func Normalize(name string, stripTrackPrefix bool) string {
	return normalize.Normalize(name, stripTrackPrefix)
}
