// Package wrapper supervises `wrapper-lite-rootless`: run it, log in to it, and shut it
// down.
//
// The hub runs the wrapper itself rather than talking to a wrapper somebody else started,
// for one reason: **2FA**. Upstream's container entrypoint reads the 2FA code out of a file
// on the host, which a web form cannot fill in. Driving the launcher, the hub can see its
// console output, recognise the moment it asks for a code, and produce that file.
//
// The design is narrower than it first looks (`wrapper/lite/auth.cpp` is explicit about
// what it accepts):
//
//   - Credentials arrive on **argv**, as `--login user:pass` (`lite_main.cpp:516-518`, the
//     only `set_credentials` call site). Nothing reads them from stdin.
//   - The 2FA code arrives **in a file**, `<base-dir>/2fa.txt` (`auth.cpp:62`, `78`), and
//     only when `--code-from-file` is set or stdin is not a tty. The interactive stdin
//     branch (`auth.cpp:64`) is gated on `isatty(STDIN_FILENO)`, and a child this
//     supervisor spawns has a pipe there, so that branch is unreachable from here and the
//     file is the only path that exists. The child polls for it `20 x sleep(3)` and then
//     gives up (`auth.cpp:83-88`), and `remove()`s the file itself once read
//     (`auth.cpp:99`).
//
// So `Login` runs a **separate, short-lived login child** -- the launcher is invoked again
// in its `--login` mode -- and `Submit2FA` creates the file that child is waiting for.
// That is upstream's own flow: `wrapper/gui/main.go`'s `performLogin` builds exactly this
// argv and watches the same output for the same markers.
//
// **This is specific to the two rootfs launchers.** The `<base-dir>/2fa.txt` written here
// has to land on a path the child can see, which means reproducing the launcher's
// `chdir("./rootfs")` + `chroot(".")` (`wrapper-lite-rootless.c:131-138`) on the host: the
// file goes to `<binary's directory>/rootfs/<base-dir>/2fa.txt`. That mapping is correct
// for `wrapper-lite-rootless` and for `wrapper-lite` and **cannot work for
// `wrapper-lite-qemu`**, which has no `rootfs` at all and passes `--base-dir` into the
// guest, where the file would live in a namespace the host cannot see. A QEMU deployment
// logs in wrapper-side; `Submit2FA` refuses with a message saying so rather than writing a
// file nobody will read.
//
// **Credentials on argv are visible in `/proc/<pid>/cmdline`** to any process of the same
// uid for as long as the child lives. That is a real exposure and the design does not get
// around it; the payload takes no other input. What bounds it is the topology -- one
// service process per container, and the login child's lifetime is seconds -- and the
// mitigations: credentials never reach the log sink (every line the pump forwards is
// scrubbed), never reach this process's own argv or environment, and never reach the
// database.
//
// Four behaviours are load-bearing, all four measured against the real launcher rather
// than designed:
//
//   - R6 -- readiness is `GET /status`, never log text. The launcher prints its banner
//     before it serves, and the measured gap was 5.9-18.7 s, so a log-based gate reports
//     ready up to nineteen seconds early and the first download fails. Ready means more
//     than a 200: the payload reports `regions: []` until an account is logged in, so
//     readiness also requires a non-empty `regions`. The two ways of failing are reported
//     separately, because they need different things from the user: "it never came up"
//     and "it came up and there is no account on it".
//   - R7 -- `Stop` signals the launcher pid, never the process group. The launcher
//     `unshare`s `CLONE_NEWPID`, so the payload is PID 1 of a nested PID namespace where
//     a `killpg` from outside does not mean what it looks like it means. The launcher
//     forwards SIGTERM to its chrooted child, which consumes it via `sigwait`.
//   - R8 -- adopt a healthy wrapper that is already on the port. This host already runs
//     one on 127.0.0.1:12340 and the user's own vendor config points there, so local
//     development collides constantly. Adoption is also the only safe answer: the
//     payload's listening socket takes `SO_REUSEPORT` and never `SO_REUSEADDR`, so a
//     second launcher on the same port would *share* it and `/status` could be answered
//     by either process.
//   - A port pre-flight, because a bind failure is not reportable from the child's side.
//     `svr.listen()` returning at once makes `lite_main.cpp:705` signal *itself*, so the
//     log says `received signal 15, stopping service` -- a line that reads exactly like an
//     external kill. `Start` therefore refuses before spawning when the port is held by
//     something that does not answer `/status`.
//
// This is the Go port of `hub/hub/wrapper_supervisor.py`, and the Python module remains
// the source of truth for anything not said here.
package wrapper

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sync"
	"syscall"
	"time"
)

// How often readiness is re-probed: well under the 0.5 s a `/key` request can take, far
// above the cost of a loopback GET, and it bounds how long a death goes unnoticed.
const PollInterval = 200 * time.Millisecond

// Readiness probes and the pre-flight are short-timeout on purpose. `startupTimeout`
// governs how long the wrapper gets to come up; a hung probe must not eat that budget.
const ProbeTimeout = 2 * time.Second

// A connect() to a closed loopback port is refused immediately, so this is only ever
// reached by something that is actually holding the port.
const ConnectTimeout = time.Second

// How long a login child is given to reach its 2FA prompt. A prompt follows the credential
// handoff within seconds when it is coming at all, and the login child is a separate
// process that exits on its own, so this only bounds how long we wait to notice.
const LoginPromptTimeout = 30 * time.Second

// How long SIGTERM is given before SIGKILL. The graceful path is the launcher forwarding
// to a chrooted child shutting down an HTTP server, measured as immediate; 15 s is
// generous for a machine under load and still bounded.
const StopTimeout = 15 * time.Second

// Exponential backoff between automatic restarts, and its ceiling. Restarts are capped and
// an unbounded loop is forbidden, so this only has to be short enough that a genuinely
// dead launcher is reported promptly.
const (
	RestartBackoffBase = 500 * time.Millisecond
	RestartBackoffCap  = 8 * time.Second
)

// LogTailLines is how many lines of the child's own output are quoted back in an error:
// enough to include the namespace-setup `perror` that is usually the whole story and the
// listen banner, without pasting a screenful into an error message.
const LogTailLines = 12

// ReadChunk is how much the log pump asks the pipe for at a time. A pipe read returns
// whatever is buffered, so this is an upper bound on a burst rather than a wait -- which
// is what lets a log line arrive promptly enough to trigger a challenge.
const ReadChunk = 4096

// Redacted replaces every known credential value on its way to the log.
const Redacted = "***"

// SelfSignal is the EADDRINUSE self-signal, the one child log line that looks like
// something else entirely: `lite_main.cpp:705` makes the payload signal *itself* when
// `svr.listen()` fails, so the log says `received signal 15, stopping service` -- which
// reads exactly like an external kill. The supervisor names it wherever it explains a
// death, and `cmd/amdhub` references it so the constant is where the diagnosis is made.
const SelfSignal = "received signal 15"

// Config is what the supervisor needs to run one launcher.
//
// The tunables a caller rarely changes are set by `New` rather than carried here, so the
// zero value of this struct is never half-usable.
type Config struct {
	// Binary is the launcher to spawn, e.g. `/opt/wrapper/wrapper-lite-rootless`.
	Binary string
	// BaseDir is the launcher's `--base-dir`, resolved *inside* its chroot (see the
	// package doc: the 2FA file lives at `<binary's dir>/rootfs/<base-dir>/2fa.txt`).
	BaseDir string
	// Host and Port are where the payload will listen. Port 0 means "pick an ephemeral
	// port", because learning it from the child's log output would be exactly the
	// log-text gate R6 forbids.
	Host string
	Port int
	// LogSink receives every line the supervisor or the child produces. It is called
	// from pump goroutines and must tolerate that; it is never allowed to kill one (see
	// `emit`).
	LogSink func(string)
	// AdoptExisting allows R8: a healthy wrapper already on the port is adopted rather
	// than fought over.
	AdoptExisting bool
}

// Error is anything the caller of a supervisor is expected to show a user.
//
// Every message is written to be shown: it names the URL, the pid, or the launcher's own
// last words, because the failure modes of this child are not visible from the parent's
// side. The type exists so a caller printing `%T` can name the collaborator that failed.
type Error struct{ message string }

func (e *Error) Error() string { return e.message }

func fail(format string, args ...any) error {
	return &Error{message: fmt.Sprintf(format, args...)}
}

// childExited is internal: the child died, so the attempt is worth repeating. A distinct
// type rather than a flag on `Error`, so only a *death* is ever retried -- see `Start` and
// `restartAfterCrash`.
type childExited struct{ message string }

func (e *childExited) Error() string { return e.message }

// Challenge is a pending 2FA code, identified for `Submit2FA` and expiring at the
// supervisor's TTL.
//
// `ExpiresAt` is wall-clock time, not a monotonic reading: it is shown to a user ("this
// code is stale, log in again") and compared against the wall clock in `Submit2FA`, so it
// has to mean the same thing in both places.
type Challenge struct {
	ID        string
	ExpiresAt time.Time
}

// Supervisor starts, watches, logs in to, and stops one `wrapper-lite-rootless`.
//
// Not a singleton: one instance per wrapper. It is safe for concurrent use -- the HTTP
// handlers and the scheduler all ask it things -- and the two long operations (`Start` and
// `Stop`) serialise against each other rather than interleaving spawns.
type Supervisor struct {
	binary        string
	baseDir       string
	host          string
	port          int
	logSink       func(string)
	adoptExisting bool

	// Tunables, fixed by `New`: the challenge TTL (clamped to the child's own 60 s
	// window), the restart budget, and the readiness deadline.
	twoFATTL      time.Duration
	maxRestarts   int
	startupTimeout time.Duration

	// spawnMu serialises the long operations: `Start`, `Stop`, and the crash watcher's
	// re-spawn. A double spawn would put two launchers on one port -- which
	// `SO_REUSEPORT` would allow, and which would make `/status` untrustworthy.
	spawnMu sync.Mutex

	mu            sync.Mutex
	service       *child
	login         *child
	adopted       bool
	boundPort     int
	stopping      bool
	restartsUsed  int
	secrets       map[string]struct{}
	challenges    map[string]time.Time
	client        *http.Client
	watchGeneration int
}

// New builds a supervisor. Nothing is spawned until `Start`.
func New(config Config) *Supervisor {
	return &Supervisor{
		binary:        config.Binary,
		baseDir:       config.BaseDir,
		host:          config.Host,
		port:          config.Port,
		logSink:       config.LogSink,
		adoptExisting: config.AdoptExisting,
		// The default TTL is the child's own window, not a round number: `auth.cpp:83-88`
		// polls `20 x sleep(3)` and then aborts the login, so a code offered after that
		// cannot be read however promptly the user types it. A longer TTL would promise a
		// deadline the wrapper does not honour.
		twoFATTL:      60 * time.Second,
		maxRestarts:   3,
		startupTimeout: 60 * time.Second,
		secrets:       map[string]struct{}{},
		challenges:    map[string]time.Time{},
	}
}

// -- observable state ---------------------------------------------------------

// Running is whether a wrapper is available to serve requests right now.
//
// True for an adopted instance too: the question a caller asks is "can I use a wrapper",
// and the answer is yes whether this supervisor started it or not. `Adopted` is how a
// caller tells the two apart -- the UI has to, because an adopted one cannot be logged
// into.
func (s *Supervisor) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adopted || (s.service != nil && s.service.alive())
}

// Adopted is true when this supervisor drives a wrapper it did not start (R8).
func (s *Supervisor) Adopted() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adopted
}

// Pid is the serving launcher's pid, or 0 when there is no live child to signal.
//
// Zero rather than a dead pid because the only use for it is signalling, and R7 makes
// that the sharpest edge in this package.
func (s *Supervisor) Pid() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.service == nil || !s.service.alive() {
		return 0
	}
	return s.service.pid
}

// BoundPort is the port the wrapper is actually on.
//
// Differs from the requested port whenever `Config.Port` is 0, which means "bind an
// ephemeral port": the supervisor picks one, because learning it from the child's log
// output would be exactly the log-text gate R6 forbids. 0 before the first successful
// start.
func (s *Supervisor) BoundPort() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.boundPort
}

// -- lifecycle ----------------------------------------------------------------

// Start brings the wrapper up, or returns an `Error` saying why not.
//
// Adopts an existing healthy wrapper when `AdoptExisting` is set, otherwise spawns. A
// failure always leaves no child behind: the point of `Start` returning an error is that
// the UI can offer a retry, and a silently surviving half-started wrapper would hold the
// port against that retry.
//
// Retries, both here and in the crash watcher, are for a child that *died*, at most
// `maxRestarts` times with exponential backoff, and never for anything else. In
// particular a readiness **timeout is not retried**: the child is alive and may simply be
// one of the 19-second startups the spike measured, and respawning it would turn a slow
// start into an infinite one. An occupied port or a missing binary is reported
// immediately, because retrying either changes nothing.
func (s *Supervisor) Start(ctx context.Context) error {
	if s.Running() {
		return nil
	}
	s.spawnMu.Lock()
	defer s.spawnMu.Unlock()

	s.mu.Lock()
	s.restartsUsed = 0
	s.mu.Unlock()

	for attempt := 1; attempt <= s.maxRestarts+1; attempt++ {
		if err := s.ensureChild(ctx); err != nil {
			return err
		}
		readyErr := s.waitReady(ctx)
		if readyErr == nil {
			s.mu.Lock()
			s.stopping = false
			bound, pid := s.boundPort, 0
			if s.service != nil {
				pid = s.service.pid
			}
			s.mu.Unlock()
			s.emit("the wrapper is ready on %s (pid %d, port %d)", s.statusURL(bound), pid, bound)
			s.startWatching()
			return nil
		}

		var died *childExited
		if !asChildExited(readyErr, &died) {
			// Not retried, by the reasoning in the docstring. Torn down rather than left
			// half-alive, so the caller can retry cleanly.
			s.shutdownService()
			return readyErr
		}

		if attempt > s.maxRestarts {
			message := died.message
			s.shutdownService()
			return fail("%s", message)
		}
		s.mu.Lock()
		s.restartsUsed = attempt
		s.mu.Unlock()
		delay := backoff(attempt)
		s.emit("the wrapper exited while starting up; restarting in %.1fs (attempt %d of %d)",
			delay.Seconds(), attempt, s.maxRestarts+1)
		if err := sleepCtx(ctx, delay); err != nil {
			s.shutdownService()
			return err
		}
	}
	return nil // unreachable: the loop returns on every path
}

func asChildExited(err error, target **childExited) bool {
	if typed, ok := err.(*childExited); ok {
		*target = typed
		return true
	}
	return false
}

// Stop shuts down what this supervisor started, and nothing else. Idempotent.
//
// An adopted wrapper is left running: it is somebody else's process, and on this host it
// is a service the user started by hand. Both children -- the serving one and any login
// child -- get SIGTERM on their own pid and are reaped before this returns, so a caller
// that checks `kill(pid, 0)` afterwards is not looking at a zombie.
func (s *Supervisor) Stop() {
	s.spawnMu.Lock()
	defer s.spawnMu.Unlock()

	s.mu.Lock()
	s.stopping = true
	s.watchGeneration++ // any watcher is now stale
	s.mu.Unlock()

	s.shutdownService()
	s.shutdownLogin()
	s.mu.Lock()
	s.adopted = false
	s.mu.Unlock()
}

// Status is the `data` object of `GET /status`, e.g. `{"regions": ["jp"]}`.
//
// Uncached, unlike the downloader's client: the hub re-reads it to drive the "regions went
// empty" path, which is a state change a cache would hide.
func (s *Supervisor) Status(ctx context.Context) (map[string]any, error) {
	s.mu.Lock()
	bound := s.boundPort
	adopted := s.adopted
	service := s.service
	s.mu.Unlock()

	if bound == 0 {
		return nil, fail("the supervisor is not started yet, so there is no wrapper to ask")
	}
	if !adopted && (service == nil || !service.alive()) {
		// Deliberately not keyed on `boundPort`, which `pickPort` sets *before* the
		// spawn: after a failed `Start` the port is known and nothing is listening, and
		// the caller would get a connection failure for what is really "it is not
		// running".
		pid, code := "none", "n/a"
		if service != nil {
			pid = fmt.Sprintf("%d", service.pid)
			code = fmt.Sprintf("%d", service.exitCode())
		}
		return nil, fail("no wrapper is running to ask, so there is nothing at %s. The last "+
			"attempt at pid %s exited with code %s; start it again.",
			s.statusURL(bound), pid, code)
	}
	probe := s.probeStatus(ctx, bound)
	if !probe.usable {
		return nil, fail("%s is not answering with a usable status: %s",
			s.statusURL(bound), probe.detail)
	}
	return probe.data, nil
}

// -- the wrapper's HTTP endpoint ----------------------------------------------

// probe is one `GET /status` attempt, kept whole so the failure message can be precise.
//
// `answered` and `usable` are separate because they answer different questions: "did
// anything speak HTTP on this port" and "is what it said a wrapper status". The difference
// is what separates "still starting up" from "not a wrapper at all".
type probe struct {
	answered bool
	usable   bool
	regions  []any
	data     map[string]any
	detail   string
}

func (s *Supervisor) statusURL(port int) string {
	return fmt.Sprintf("http://%s:%d/status", s.host, port)
}

// httpClient is shared across probes. It never uses the environment's proxy: the wrapper
// is loopback-only and must never be reached through one, and a proxy that answers 200 for
// anything would fake readiness.
func (s *Supervisor) httpClient() *http.Client {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.client == nil {
		s.client = &http.Client{
			Timeout:   ProbeTimeout,
			Transport: &http.Transport{Proxy: nil},
		}
	}
	return s.client
}

// probeStatus is one `GET /status`, decomposed into the three questions a caller has.
//
// The envelope handling is the same as `AppleMusicDecrypt/src/wrapper.py`'s
// `_decode_response`, deliberately: the hub and the CLI have to agree on what a healthy
// instance looks like, or the same wrapper reads as up in one and down in the other.
func (s *Supervisor) probeStatus(ctx context.Context, port int) probe {
	url := s.statusURL(port)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return probe{detail: fmt.Sprintf("GET %s failed: %v", url, err)}
	}
	response, err := s.httpClient().Do(request)
	if err != nil {
		return probe{detail: fmt.Sprintf("GET %s failed: %v", url, err)}
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		return probe{answered: true, detail: fmt.Sprintf("GET %s returned HTTP %d", url, response.StatusCode)}
	}
	var payload map[string]any
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return probe{answered: true, detail: fmt.Sprintf("GET %s returned a non-JSON body", url)}
	}
	code, hasCode := payload["code"]
	if !hasCode {
		return probe{answered: true, detail: fmt.Sprintf("GET %s returned an unexpected envelope", url)}
	}
	if number, ok := code.(float64); !ok || number != 0 {
		return probe{answered: true,
			detail: fmt.Sprintf("GET %s returned code=%v msg=%v", url, payload["code"], payload["msg"])}
	}
	data, _ := payload["data"].(map[string]any)
	if data == nil {
		data = map[string]any{}
	}
	regions, _ := data["regions"].([]any)
	return probe{
		answered: true,
		usable:   true,
		regions:  regions,
		data:     data,
		detail:   "HTTP 200, code=0",
	}
}

// -- starting -----------------------------------------------------------------

// ensureChild makes sure a live serving child exists, spawning one if it does not.
// The caller holds `spawnMu`; a double spawn would put two launchers on one port.
func (s *Supervisor) ensureChild(ctx context.Context) error {
	s.mu.Lock()
	if s.service != nil && s.service.alive() {
		s.mu.Unlock()
		return nil
	}
	old := s.service
	s.service = nil
	s.stopping = false
	s.mu.Unlock()

	s.reap(old)
	return s.spawnService(ctx)
}

func (s *Supervisor) spawnService(ctx context.Context) error {
	adopted, err := s.preflight(ctx)
	if err != nil {
		return err
	}
	if adopted {
		return nil
	}

	s.mu.Lock()
	s.boundPort = s.pickPortLocked()
	bound := s.boundPort
	s.mu.Unlock()

	argv := []string{
		s.binary,
		"--base-dir", s.baseDir,
		"--host", s.host,
		"--port", fmt.Sprintf("%d", bound),
	}
	// Logged with the port and nothing else: this line goes to the UI, and a serving
	// child never carries credentials.
	s.emit("spawning %s on %s:%d", argv[0], s.host, bound)
	child, err := s.spawn(argv, "service")
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.service = child
	s.mu.Unlock()
	return nil
}

// preflight decides between adopting, refusing, and spawning. True means "adopted".
//
// Only meaningful for an explicit port. Port 0 means "pick one for me", so there is
// nothing to inspect and nothing to collide with.
func (s *Supervisor) preflight(ctx context.Context) (bool, error) {
	if s.port == 0 {
		return false, nil
	}

	result := s.probeStatus(ctx, s.port)
	if result.usable {
		s.mu.Lock()
		adoptExisting := s.adoptExisting
		s.mu.Unlock()
		if !adoptExisting {
			return false, fail("port %d on %s is already served by a wrapper answering "+
				"/status, and adoption is switched off; point AMD_WRAPPER_PORT somewhere "+
				"else or enable adoption", s.port, s.host)
		}
		s.mu.Lock()
		s.adopted = true
		s.boundPort = s.port
		s.mu.Unlock()
		s.emit("adopted the wrapper already serving on %s; logins have to happen on the "+
			"wrapper side", s.statusURL(s.port))
		return true, nil
	}

	if s.portHasListener(s.port) {
		return false, fail("port %d on %s is in use by another process, which does not "+
			"answer /status (%s). Refusing to start a launcher there: it would fail its "+
			"bind with EADDRINUSE and log '%s', which is indistinguishable from an "+
			"external kill.", s.port, s.host, result.detail, SelfSignal)
	}
	return false, nil
}

// portHasListener reports whether something accepts a connection on the port.
//
// A refused connection means nothing is there, *including* a TIME_WAIT remnant: those have
// no listener, and the payload binds with `SO_REUSEPORT` so a remnant does not block it. A
// connect that times out is counted as occupied, because on loopback a connect either
// succeeds or is refused at once.
func (s *Supervisor) portHasListener(port int) bool {
	address := net.JoinHostPort(s.host, fmt.Sprintf("%d", port))
	connection, err := net.DialTimeout("tcp", address, ConnectTimeout)
	if err != nil {
		if isTimeout(err) {
			return true
		}
		return false
	}
	_ = connection.Close()
	return true
}

func (s *Supervisor) pickPortLocked() int {
	if s.port != 0 {
		return s.port
	}
	// Bind-and-release, exactly as the spike probe picked its port. Inherently racy for
	// the microseconds between the close and the child's bind, and harmless: the
	// payload's `SO_REUSEPORT` means a lost race is a shared port rather than a failure,
	// and the readiness probe would then be answered by the winner.
	listener, err := net.Listen("tcp", net.JoinHostPort(s.host, "0"))
	if err != nil {
		return 0
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	return port
}

// -- readiness ----------------------------------------------------------------

// waitReady polls `/status` until it reports regions, or gives up. R6, in one place.
//
// Nothing here reads the child's output to decide readiness; the only thing the output is
// used for is being reported when this fails.
func (s *Supervisor) waitReady(ctx context.Context) error {
	deadline := time.Now().Add(s.startupTimeout)
	lastDetail := "no probe was completed"
	servingWithoutRegions := false

	for {
		if err := s.raiseIfDead(); err != nil {
			return err
		}
		s.mu.Lock()
		bound := s.boundPort
		s.mu.Unlock()
		result := s.probeStatus(ctx, bound)
		lastDetail = result.detail
		if result.usable && len(result.regions) > 0 {
			return nil
		}
		if result.usable && !servingWithoutRegions {
			servingWithoutRegions = true
			// Said once, not every poll: the payload serves this state for as long as no
			// account is logged in, which is the state a fresh install boots into.
			s.emit("the wrapper is serving on %s but reports no regions: it is up and "+
				"healthy, there is just no Apple account logged in on it yet, so the hub "+
				"should offer to log in", s.statusURL(bound))
		}
		if err := s.raiseIfDead(); err != nil {
			return err
		}

		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		if err := sleepCtx(ctx, minDuration(PollInterval, remaining)); err != nil {
			return err
		}
	}

	s.mu.Lock()
	bound := s.boundPort
	service := s.service
	s.mu.Unlock()
	if servingWithoutRegions {
		// The two cases need different things from the user, so they are worded
		// differently. This one is not "come back in a minute": the wrapper is already
		// serving, and the only thing missing is an account.
		return fail("no account is logged in on the wrapper at %s: it is up and answering "+
			"/status, but regions is empty, so it cannot serve a download. Log in and "+
			"start it again. Nothing needs to be waited for -- the wrapper itself is "+
			"ready.", s.statusURL(bound))
	}
	return fail("the wrapper did not become ready within %.0fs: %s never returned a usable "+
		"status envelope (last probe: %s). Its own output was:\n%s",
		s.startupTimeout.Seconds(), s.statusURL(bound), lastDetail, tailText(service))
}

// raiseIfDead turns "the child exited" into a message that says what it printed.
//
// Draining the pump first is what makes the message useful: the pipe still holds the
// child's last words at the moment it exits, and a `perror` on the way out is usually the
// entire diagnosis.
func (s *Supervisor) raiseIfDead() error {
	s.mu.Lock()
	service := s.service
	s.mu.Unlock()
	if service == nil || service.alive() {
		return nil
	}
	s.drainPump(service)
	return &childExited{message: fmt.Sprintf(
		"the wrapper launcher (pid %d) exited with code %d before the wrapper was ready.%s "+
			"Its output was:\n%s",
		service.pid, service.exitCode(), diagnoseTail(service, s.host, s.boundPort), tailText(service))}
}

// -- the crash watcher --------------------------------------------------------

func (s *Supervisor) startWatching() {
	s.mu.Lock()
	s.watchGeneration++
	generation := s.watchGeneration
	service := s.service
	s.mu.Unlock()
	if service == nil {
		return
	}
	go s.watchService(generation, service)
}

func (s *Supervisor) watchService(generation int, watched *child) {
	<-watched.done
	s.mu.Lock()
	stale := s.stopping || s.watchGeneration != generation || s.service != watched
	bound := s.boundPort
	s.mu.Unlock()
	if stale {
		return
	}
	s.emit("the wrapper exited unexpectedly with code %d; it is no longer running.%s",
		watched.exitCode(), diagnoseTail(watched, s.host, bound))
	s.restartAfterCrash()
}

// restartAfterCrash respawns a crashed wrapper with backoff, up to the budget, then gives
// up.
//
// An unexpected exit is a restart with exponential backoff, three times, and after that it
// is the user's problem rather than a loop. The budget is shared with `Start`'s own retry
// loop, so the supervisor will not spawn more than `maxRestarts` extra children per epoch
// however the deaths arrive. A restart is only counted as a success once the wrapper is
// *serving again* -- the same `/status` gate `Start` uses.
func (s *Supervisor) restartAfterCrash() {
	s.spawnMu.Lock()
	defer s.spawnMu.Unlock()

	for {
		s.mu.Lock()
		if s.stopping {
			s.mu.Unlock()
			return
		}
		if s.restartsUsed >= s.maxRestarts {
			s.service = nil
			used, budget := s.restartsUsed, s.maxRestarts
			s.mu.Unlock()
			s.emit("the wrapper will not be restarted: the automatic restart budget is "+
				"spent (%d of %d used) and the last attempt did not come back. Start it "+
				"again by hand, and expect the same failure until whatever is causing it "+
				"is fixed.", used, budget)
			return
		}
		attempt := s.restartsUsed + 1
		s.mu.Unlock()

		delay := backoff(attempt)
		s.emit("restarting the wrapper in %.1fs (automatic restart %d of %d)",
			delay.Seconds(), attempt, s.maxRestarts)
		if !sleepUnstoppable(s, delay) {
			return
		}
		s.mu.Lock()
		s.restartsUsed = attempt
		s.mu.Unlock()

		ctx := context.Background()
		err := s.ensureChild(ctx)
		if err == nil {
			err = s.waitReady(ctx)
		}
		if err != nil {
			s.emit("automatic restart %d did not work: %v", attempt, err)
			s.shutdownService()
			continue
		}
		s.mu.Lock()
		s.stopping = false
		bound, pid := s.boundPort, 0
		if s.service != nil {
			pid = s.service.pid
		}
		s.mu.Unlock()
		s.emit("the wrapper is serving again on %s (pid %d, port %d)",
			s.statusURL(bound), pid, bound)
		s.startWatching()
		return
	}
}

func backoff(attempt int) time.Duration {
	delay := RestartBackoffBase
	for i := 1; i < attempt; i++ {
		delay *= 2
		if delay >= RestartBackoffCap {
			return RestartBackoffCap
		}
	}
	if delay > RestartBackoffCap {
		return RestartBackoffCap
	}
	return delay
}

// -- the log sink -------------------------------------------------------------

// emit is one formatted line to `LogSink`, and never an exception out of it.
//
// The API layer's log sink appends to a log pane and publishes to a stream, so it can fail
// for reasons that have nothing to do with the wrapper. Letting that reach the pump would
// kill the reader.
func (s *Supervisor) emit(format string, args ...any) {
	s.mu.Lock()
	sink := s.logSink
	s.mu.Unlock()
	if sink == nil {
		return
	}
	defer func() { _ = recover() }()
	sink(fmt.Sprintf(format, args...))
}

// -- shared helpers ------------------------------------------------------------

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

// sleepCtx sleeps, or returns early when the caller's context ends.
func sleepCtx(ctx context.Context, duration time.Duration) error {
	if duration <= 0 {
		return nil
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// sleepUnstoppable sleeps unless `Stop` starts: a crash-restart backoff must not keep a
// stopped supervisor waiting to die. False means it stopped early.
//
// Polled rather than signalled because `Stop` may run before or after any given sleep and
// the flag is the one thing all of them already share.
func sleepUnstoppable(s *Supervisor, duration time.Duration) bool {
	const tick = 100 * time.Millisecond
	for remaining := duration; remaining > 0; {
		slice := minDuration(tick, remaining)
		time.Sleep(slice)
		remaining -= slice
		s.mu.Lock()
		stopping := s.stopping
		s.mu.Unlock()
		if stopping {
			return false
		}
	}
	return true
}

func isTimeout(err error) bool {
	type timeout interface{ Timeout() bool }
	if typed, ok := err.(timeout); ok {
		return typed.Timeout()
	}
	return false
}

// SIGTERM and SIGKILL, named once: R7 says the launcher's own pid, never the group.
var (
	sigterm = syscall.SIGTERM
	sigkill = syscall.SIGKILL
)
