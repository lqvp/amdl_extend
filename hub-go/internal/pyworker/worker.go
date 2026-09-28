// Package pyworker is the Go hub's side of the pipe to the Python Apple client.
//
// The port covers the hub and not the client. `AppleMusicDecrypt/src/*` -- reached
// through `hub/ripper_host.py` -- is where the Apple API, the token cache and the
// FairPlay work live: the one part of this system that talks to somebody else's
// servers and is already tuned against them. Reimplementing it would drift from
// upstream in ways nobody notices until a track refuses to download, so it stays
// Python and is driven across a process boundary. The protocol is documented on the
// Python side, in `tools/pyworker.py`, which is the file that implements it.
//
// **One long-lived worker, not one process per request.** `RipperHost.start()`
// chdirs into the vendor tree, puts it on `sys.path`, registers six `creart`
// creators and builds the HTTP client and token cache that make an expansion cheap.
// Paying that per request would be a second of setup and a fresh login per album.
//
// **Requests are concurrent, responses are matched by id.** Two rips at once is the
// point of `AMD_RIP_CONCURRENCY`, and an album expansion must not block a progress
// reading. So callers do not hold a lock while waiting; each call registers a channel
// under its own id and the reader goroutine hands the answer back.
//
// **A dead worker fails every pending call rather than hanging.** The failure a hub
// operator actually hits is a worker that died at import time -- a missing dependency
// in the image, a vendor tree that is not there -- and a call that blocked for ever on
// it would look like a hung download rather than a broken deployment. `Close` and the
// reader goroutine both drain the pending map, and the error names the process's own
// exit state plus the tail of its stderr.
package pyworker

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"amdhub/internal/jobs"
)

// maxLineBytes bounds one protocol line. An artist or playlist expansion can be
// thousands of leaves in one response, and a scanner that gave up at the default 64 KB
// would report a truncated album as a parse error -- a bug that would only be seen on
// large libraries, which is the worst place to find one.
const maxLineBytes = 16 << 20

// Options is how to start the worker.
type Options struct {
	// Python is the interpreter. Defaults to `python3`.
	Python string
	// Script is `tools/pyworker.py`.
	Script string
	// Config is the vendor `config.toml` the seam reads.
	Config string
	// Dir is the directory the worker's `hub` import resolves from. Left empty in the
	// image, where the package is installed; a checkout sets it to the repo root and
	// passes `PYTHONPATH=<repo>/hub` in `Env`.
	Dir string
	// Env is the worker's environment. The hub's own environment is not inherited by
	// default, deliberately: a stray `PYTHONPATH` in the container that points at the
	// Python hub would make the worker import a *different* `hub` than the one the
	// image installed, which is a bug that looks exactly like a working worker.
	Env []string
}

// Error is a failure the worker itself reported: an exception from `expand`, a
// `ResolveError`, a rejected URL. Its message is the Python `TypeName: message` form,
// which is what the hub's job rows have always shown.
type Error struct {
	Op      string
	Message string
}

func (e *Error) Error() string {
	if e.Op == "" {
		return e.Message
	}
	return fmt.Sprintf("the %s request to the Apple client failed: %s", e.Op, e.Message)
}

// Excerpt is used by `errors.As` callers that want to tell "the client refused this"
// from "the pipe is broken".
func AsError(err error) (*Error, bool) {
	var target *Error
	ok := errors.As(err, &target)
	return target, ok
}

type response struct {
	ID     *int64          `json:"id"`
	OK     *bool           `json:"ok"`
	Result json.RawMessage `json:"result"`
	Error  *string         `json:"error"`

	Event string `json:"event"`
	Line  string `json:"line"`

	// Progress readings, present when Event == "progress".
	JobID      *int64   `json:"job_id"`
	Fraction   *float64 `json:"fraction"`
	BytesDone  *int64   `json:"bytes_done"`
	BytesTotal *int64   `json:"bytes_total"`
}

// Worker is one running Python process.
type Worker struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser

	mu      sync.Mutex
	nextID  int64
	pending map[int64]chan response
	closed  bool

	// callbacks are set before the reader starts and read from it afterwards, so no
	// lock is needed for them beyond the one that publishes the worker.
	onProgress func(jobID int64, progress jobs.Progress)
	onLog      func(line string)

	done chan struct{}
	// err is the reason the worker stopped, set once by the reader.
	err error

	stderr *lineRing
}

// Start launches the worker and its reader goroutine.
func Start(opts Options) (*Worker, error) {
	python := opts.Python
	if python == "" {
		python = "python3"
	}
	args := []string{opts.Script, "--config", opts.Config}
	cmd := exec.Command(python, args...)
	cmd.Dir = opts.Dir
	cmd.Env = opts.Env
	if len(cmd.Env) == 0 {
		cmd.Env = os.Environ()
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("could not open the Apple client's stdin: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("could not open the Apple client's stdout: %w", err)
	}
	// stderr is kept, not discarded: it is where the Python traceback of a failed
	// `import` lands, and that traceback is the only thing that explains a worker that
	// exits before answering anything.
	base := &lineRing{limit: 40}
	cmd.Stderr = base

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("could not start the Apple client (%s %s): %w",
			python, opts.Script, err)
	}
	w := &Worker{
		cmd:     cmd,
		stdin:   stdin,
		pending: map[int64]chan response{},
		done:    make(chan struct{}),
		stderr:  base,
	}
	go w.read(stdout)
	go func() {
		state, err := cmd.Process.Wait()
		_ = state
		if err != nil {
			w.fail(fmt.Errorf("the Apple client process ended: %w%s", err, w.stderrTail()))
			return
		}
		w.fail(fmt.Errorf("the Apple client process exited%s", w.stderrTail()))
	}()
	return w, nil
}

// SetProgressHandler installs the callback for progress readings. Called from the
// reader goroutine, so it must not block.
func (w *Worker) SetProgressHandler(handler func(jobID int64, progress jobs.Progress)) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.onProgress = handler
}

// SetLogHandler installs the callback for the worker's own log lines.
func (w *Worker) SetLogHandler(handler func(line string)) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.onLog = handler
}

func (w *Worker) read(stdout io.Reader) {
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64<<10), maxLineBytes)
	for scanner.Scan() {
		w.dispatch(scanner.Bytes())
	}
	if err := scanner.Err(); err != nil {
		w.fail(fmt.Errorf("reading the Apple client's output failed: %w%s", err, w.stderrTail()))
		return
	}
	// EOF without an exit status yet: the wait goroutine reports the code, so this only
	// has to make sure nothing is left waiting.
	w.fail(fmt.Errorf("the Apple client closed its output%s", w.stderrTail()))
}

func (w *Worker) dispatch(line []byte) {
	var msg response
	if err := json.Unmarshal(line, &msg); err != nil {
		// A line neither side can parse is a bug in the protocol, not in the hub, and
		// it is reported as a log line rather than as a failure: dropping the worker
		// would take every in-flight rip with it for one bad frame.
		w.log(fmt.Sprintf("the Apple client sent an unparseable line: %s", strings.TrimSpace(string(line))))
		return
	}
	if msg.Event != "" {
		switch msg.Event {
		case "progress":
			if msg.JobID != nil {
				w.progress(*msg.JobID, jobs.Progress{
					BytesDone:  derefInt(msg.BytesDone),
					BytesTotal: msg.BytesTotal,
					Fraction:   msg.Fraction,
				})
			}
		case "log":
			w.log(msg.Line)
		}
		return
	}
	if msg.ID == nil {
		return
	}
	w.mu.Lock()
	waiter, ok := w.pending[*msg.ID]
	delete(w.pending, *msg.ID)
	w.mu.Unlock()
	if ok {
		waiter <- msg
	}
}

func (w *Worker) progress(jobID int64, reading jobs.Progress) {
	w.mu.Lock()
	handler := w.onProgress
	w.mu.Unlock()
	if handler != nil {
		handler(jobID, reading)
	}
}

func (w *Worker) log(line string) {
	w.mu.Lock()
	handler := w.onLog
	w.mu.Unlock()
	if handler != nil {
		handler(line)
	}
}

// fail records the reason the worker stopped and gives every pending call an answer.
// Idempotent: the reader and the wait goroutine both call it, whichever gets there
// first wins and nothing is delivered twice.
func (w *Worker) fail(err error) {
	w.mu.Lock()
	if w.err == nil {
		w.err = err
	}
	pending := w.pending
	w.pending = map[int64]chan response{}
	closed := w.closed
	w.closed = true
	w.mu.Unlock()

	for _, waiter := range pending {
		close(waiter)
	}
	if !closed {
		close(w.done)
	}
}

// Done is closed when the worker stops, for whatever reason.
func (w *Worker) Done() <-chan struct{} { return w.done }

// Err is why the worker stopped, or nil while it is running.
func (w *Worker) Err() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.err
}

// Call sends one op and waits for its answer.
//
// The caller's `ctx` bounds the wait, and a timeout does not kill the worker: the op
// keeps running on the Python side and its answer is dropped when it arrives. That is
// deliberate -- cancelling `run_song` would leave the client's own transfer running with
// nothing reading it -- and it is why the hub's per-job deadlines are generous.
func (w *Worker) Call(ctx context.Context, op string, args any, out any) error {
	w.mu.Lock()
	if w.closed {
		err := w.err
		w.mu.Unlock()
		if err == nil {
			err = errors.New("the Apple client is not running")
		}
		return err
	}
	w.nextID++
	id := w.nextID
	waiter := make(chan response, 1)
	w.pending[id] = waiter
	w.mu.Unlock()

	payload, err := json.Marshal(map[string]any{"id": id, "op": op, "args": args})
	if err != nil {
		w.forget(id)
		return err
	}
	if _, err := w.stdin.Write(append(payload, '\n')); err != nil {
		w.forget(id)
		return fmt.Errorf("the %s request could not be sent to the Apple client: %w%s",
			op, err, w.stderrTail())
	}

	select {
	case msg, ok := <-waiter:
		if !ok {
			if err := w.Err(); err != nil {
				return err
			}
			return errors.New("the Apple client is not running")
		}
		if msg.OK != nil && !*msg.OK {
			message := "the Apple client reported a failure with no message"
			if msg.Error != nil {
				message = *msg.Error
			}
			return &Error{Op: op, Message: message}
		}
		if out == nil || msg.Result == nil {
			return nil
		}
		return json.Unmarshal(msg.Result, out)
	case <-ctx.Done():
		// Dropped, not cancelled: see the docstring.
		w.forget(id)
		return fmt.Errorf("the %s request to the Apple client timed out after %s",
			op, describeWait(ctx.Err()))
	}
}

func (w *Worker) forget(id int64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.pending, id)
}

func describeWait(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "its deadline"
	}
	return "the request was cancelled"
}

// Expand resolves one Apple Music URL into its leaves.
func (w *Worker) Expand(ctx context.Context, url, codec, language string) ([]jobs.Leaf, error) {
	var leaves []jobs.Leaf
	err := w.Call(ctx, "expand", map[string]any{
		"url": url, "codec": codec, "language": language,
	}, &leaves)
	return leaves, err
}

// RenderFilename asks the client what file name `rip_song` would write for a leaf.
//
// The duplicate check must be given this and never the tag title: `normalize` is not
// idempotent, and a check fed a title compares against a key the library does not hold.
func (w *Worker) RenderFilename(ctx context.Context, leaf jobs.Leaf, trackNumber int) (string, error) {
	var name string
	err := w.Call(ctx, "render_filename", map[string]any{
		"leaf": leafArgs(leaf), "track_number": trackNumber,
	}, &name)
	return name, err
}

// RunSong rips one track. It returns when the file is written or the client failed.
func (w *Worker) RunSong(ctx context.Context, jobID int64, leaf jobs.Leaf, force bool) error {
	return w.Call(ctx, "run_song", map[string]any{
		"job_id": jobID, "leaf": leafArgs(leaf), "force": force,
	}, nil)
}

// RunMusicVideo rips one music video, which is the Widevine path rather than FairPlay.
func (w *Worker) RunMusicVideo(ctx context.Context, jobID int64, leaf jobs.Leaf, force bool) error {
	return w.Call(ctx, "run_music_video", map[string]any{
		"job_id": jobID, "leaf": leafArgs(leaf), "force": force,
	}, nil)
}

// RegionLanguage is `region.language` from the client's own config, or "" when it does
// not name one.
//
// Asked for rather than defaulted: the library on disk was written with whatever this
// client was configured with, and metadata in a different language would never match a
// file name.
func (w *Worker) RegionLanguage(ctx context.Context) (string, error) {
	var language string
	err := w.Call(ctx, "region_language", map[string]any{}, &language)
	return language, err
}

// Status is the client's own view of the wrapper, uncached. The Go supervisor probes the
// wrapper directly for lifecycle; this exists for the client's richer payload, which the
// hub's status route reports.
func (w *Worker) Status(ctx context.Context) (map[string]any, error) {
	var payload map[string]any
	err := w.Call(ctx, "status", map[string]any{}, &payload)
	return payload, err
}

// leafArgs is the leaf as the Python dataclass spells it. The Go struct's field names
// are for Go; this is a wire shape, and the two are allowed to differ.
func leafArgs(leaf jobs.Leaf) map[string]any {
	return map[string]any{
		"adam_id":        leaf.AdamID,
		"title":          leaf.Title,
		"album_name":     leaf.AlbumName,
		"artist_name":    leaf.ArtistName,
		"codec":          leaf.Codec,
		"language":       leaf.Language,
		"url":            leaf.URL,
		"storefront":     leaf.Storefront,
		"is_music_video": leaf.IsMusicVideo,
	}
}

// Close asks the worker to shut down and waits for it, then kills it.
//
// The order matters: `shutdown` lets `RipperHost.close()` release the client's own
// threads and temp files, and the kill is only for a worker that did not take the
// hint. A hub that is shutting down during a rip gives it `DrainTimeout` first -- see
// the caller -- because SIGKILLing a rip leaves a partial file and a `running` row.
func (w *Worker) Close(timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	_ = w.Call(ctx, "shutdown", map[string]any{}, nil)
	_ = w.stdin.Close()
	select {
	case <-w.done:
	case <-time.After(2 * time.Second):
		_ = w.cmd.Process.Kill()
		<-w.done
	}
	return nil
}

func (w *Worker) stderrTail() string {
	tail := w.stderr.tail()
	if tail == "" {
		return ""
	}
	return ". Its output was:\n" + tail
}

func derefInt(value *int64) int64 {
	if value == nil {
		return 0
	}
	return *value
}

// lineRing keeps the last few lines of a stream, so an error message can quote what the
// process said without keeping its whole output in memory. Also used as an `io.Writer`,
// which is what `exec.Cmd` wants.
type lineRing struct {
	mu    sync.Mutex
	lines []string
	limit int
	buf   strings.Builder
	head  bool
}

func (r *lineRing) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, b := range p {
		if b == '\n' {
			r.lines = append(r.lines, r.buf.String())
			if len(r.lines) > r.limit {
				r.lines = r.lines[len(r.lines)-r.limit:]
			}
			r.buf.Reset()
			continue
		}
		r.buf.WriteByte(b)
	}
	return len(p), nil
}

func (r *lineRing) tail() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	lines := append([]string(nil), r.lines...)
	if r.buf.Len() > 0 {
		lines = append(lines, r.buf.String())
	}
	return strings.Join(lines, "\n")
}
