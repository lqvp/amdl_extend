package wrapper

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// child is one launcher process this supervisor started, its log pump, and the tail of
// what it said.
//
// Two of these can exist at once -- the serving child and the short-lived login child --
// and they are the same kind of thing, so they get one type rather than two parallel sets
// of fields that drift apart.
type child struct {
	cmd   *exec.Cmd
	pid   int
	label string   // "service" or "login", for the teardown and pump messages
	tail  []string // the last `LogTailLines` scrubbed lines, oldest first

	partial  string // the not-yet-a-line remainder of the child's output
	pumpDone chan struct{}

	done     chan struct{} // closed once `cmd.Wait` has returned
	waitOnce sync.Once
	exitErr  error

	twofaSeen   bool
	twofaSeenAt time.Time // when the prompt marker was seen, not when the challenge is minted
	twofaEvent  chan struct{}
	twofaOnce   sync.Once

	mu sync.Mutex
}

func (c *child) alive() bool {
	select {
	case <-c.done:
		return false
	default:
		return true
	}
}

func (c *child) exitCode() int {
	<-c.done
	if c.exitErr == nil {
		return 0
	}
	if typed, ok := c.exitErr.(*exec.ExitError); ok {
		return typed.ExitCode()
	}
	return -1
}

// spawn creates one launcher process and starts reading its output.
//
// `cwd` is the binary's directory, never the inherited one: `wrapper-lite-rootless.c`
// chroots into `./rootfs` and resolves `--base-dir` *after* `chroot(".")`, both relative
// to the CWD. stderr is folded into stdout: every `LOG_*` line goes to stderr
// (`wrapper/lite/logger.h:59`), unbuffered, and the two must be read as one ordered stream
// to see a prompt where it appears. stdin is DEVNULL, not a pipe: there is nothing to
// write here -- the credentials are on argv and the code is in a file -- so the only thing
// stdin has to be is something the payload's `isatty` test fails, which is the point of it.
func (s *Supervisor) spawn(argv []string, label string) (*child, error) {
	directory := "."
	if index := strings.LastIndex(argv[0], "/"); index >= 0 {
		directory = argv[0][:index]
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = directory
	return s.startChild(cmd, label)
}

// startChild wires the pipe, starts the process, and begins pumping.
//
// The pipe exists before the process does, and it is an `os.Pipe` of our own rather than
// `exec.Cmd.StdoutPipe`: that one gets its read end closed by `Wait`, which races the
// pump. With our own pipe the child holds the write end until it exits, the pump reads to
// real EOF, and `Wait` reaps without touching either -- which is what makes the drain
// after exit a drain and not a hope.
func (s *Supervisor) startChild(cmd *exec.Cmd, label string) (*child, error) {
	reader, writer, err := os.Pipe()
	if err != nil {
		return nil, fail("could not create the pipe for the launcher's output: %v", err)
	}
	cmd.Stdout = writer
	cmd.Stderr = writer // merged: one ordered stream, as the GUI reads it
	if cmd.Stdin == nil {
		if devnull, err := os.Open(os.DevNull); err == nil {
			cmd.Stdin = devnull
			defer func() { _ = devnull.Close() }() // the child has its own fd after Start
		}
	}
	if err := cmd.Start(); err != nil {
		_ = reader.Close()
		_ = writer.Close()
		return nil, fail("could not start the wrapper launcher %s: %v. The launcher must "+
			"exist and be executable, and its parent directory must contain the rootfs it "+
			"chroots into.", cmd.Path, err)
	}
	_ = writer.Close() // only the child writes now; the pump sees EOF once it exits

	result := &child{
		cmd:        cmd,
		pid:        cmd.Process.Pid,
		label:      label,
		pumpDone:   make(chan struct{}),
		done:       make(chan struct{}),
		twofaEvent: make(chan struct{}),
	}
	go s.pumpOutput(result, reader)
	go func() {
		result.waitOnce.Do(func() {
			result.exitErr = cmd.Wait()
			close(result.done)
		})
	}()
	return result, nil
}

// pumpOutput reads one child's output, scrubs it, and hands it to `LogSink`.
//
// This goroutine must not die: the child writes to a pipe nobody else drains, so a pump
// that stops turns a working wrapper into a wedged one within one pipe buffer. Every
// failure here is reported into the sink and ends the loop.
//
// Chunked reads, not a line scanner: the lines this supervisor acts on are not guaranteed
// to arrive complete, and checking the *accumulated remainder* rather than the raw chunk
// additionally means a marker split across two reads is still found.
func (s *Supervisor) pumpOutput(c *child, reader *os.File) {
	defer close(c.pumpDone)
	defer func() { _ = reader.Close() }()
	buffer := make([]byte, ReadChunk)
	for {
		read, err := reader.Read(buffer)
		if read > 0 {
			c.mu.Lock()
			c.partial += string(buffer[:read])
			for {
				index := strings.IndexByte(c.partial, '\n')
				if index < 0 {
					break
				}
				line := strings.TrimSuffix(c.partial[:index], "\r")
				rest := c.partial[index+1:]
				c.partial = rest
				s.deliver(c, line)
				s.check2FA(c, line)
			}
			s.check2FA(c, c.partial)
			c.mu.Unlock()
		}
		if err != nil {
			if err != io.EOF {
				s.emit("the wrapper log pump for %s stopped: %v", labelOf(c), err)
			}
			break
		}
	}
	c.mu.Lock()
	if c.partial != "" {
		s.deliver(c, c.partial)
		c.partial = ""
	}
	c.mu.Unlock()
}

// labelOf names the pump's child in the rare line that needs it. The label is set at
// spawn; it is kept on the child so the pump and the teardown messages agree.
func labelOf(c *child) string {
	if c.label == "" {
		return "child"
	}
	return c.label
}

// deliver scrubs one line, keeps it for diagnostics, and forwards it.
//
// The redaction happens *here*, on the only path to `LogSink`. It matters most because
// credentials are on argv: the child can echo its own command line, and a crash report
// that quotes argv would put the password in the UI otherwise.
func (s *Supervisor) deliver(c *child, line string) {
	scrubbed := s.scrub(line)
	c.tail = append(c.tail, scrubbed)
	if len(c.tail) > LogTailLines {
		c.tail = c.tail[len(c.tail)-LogTailLines:]
	}
	// Blank lines dropped, matching the upstream writer: the log pane is for content.
	if strings.TrimSpace(scrubbed) != "" {
		s.emit("%s", scrubbed)
	}
}

// scrub replaces every known credential value with `Redacted`.
//
// Longest first, and that ordering is the whole correctness of this function: a username
// that is a prefix of its own password (`SECRET` / `SECRET_PASS`) would otherwise be
// replaced first and leave `***_PASS` in the log. Every value is replaced whatever its
// length, so a one-character password mangles every line that happens to contain that
// character -- the safe direction to fail in.
func (s *Supervisor) scrub(text string) string {
	s.mu.Lock()
	secrets := make([]string, 0, len(s.secrets))
	for secret := range s.secrets {
		secrets = append(secrets, secret)
	}
	s.mu.Unlock()
	sortByLengthDesc(secrets)
	for _, secret := range secrets {
		text = strings.ReplaceAll(text, secret, Redacted)
	}
	return text
}

func (s *Supervisor) rememberSecret(value string) {
	if value == "" {
		return
	}
	s.mu.Lock()
	s.secrets[value] = struct{}{}
	s.mu.Unlock()
}

// check2FA is the 2FA prompt heuristic, copied from `wrapper/gui/main.go:653-657`
// (`check2FA`) rather than reinvented. These are the only five strings the upstream GUI
// treats as a 2FA request.
//
// Called with the child's lock held.
func (s *Supervisor) check2FA(c *child, text string) {
	if c.twofaSeen {
		return
	}
	for _, marker := range twoFAMarkers {
		if strings.Contains(text, marker) {
			c.twofaSeen = true
			// Stamped here rather than when the challenge is minted: this is the moment
			// the child starts counting, so this is the only timestamp the deadline may
			// be measured from.
			c.twofaSeenAt = time.Now()
			c.twofaOnce.Do(func() { close(c.twofaEvent) })
			return
		}
	}
}

// namespaceFailures: `wrapper-lite-rootless.c` reports every namespace failure as a bare
// `perror()` and exits. These are the strings, so a start-up failure can say which one it
// was instead of leaving the operator to guess from "Operation not permitted".
var namespaceFailures = []struct{ marker, explanation string }{
	{"unshare:", "the kernel refused to create the namespaces; the container needs " +
		"security_opt: [seccomp:unconfined, systempaths=unconfined]"},
	{"mount proc failed", "mounting a fresh procfs inside the user namespace was " +
		"refused; that is the /proc over-mount that systempaths=unconfined removes"},
	{"mount /dev/urandom failed", "the random device could not be bound into the chroot"},
	{"mkdir ./rootfs", "the launcher could not create its own rootfs entries, which is " +
		"what a uid-mismatched bind mount looks like"},
	{"open ./rootfs", "the rootfs is not readable by the mapped uid"},
	{"chroot", "the chroot into ./rootfs was refused"},
	{"execve", "the payload could not be exec'd; usually a library mismatch, e.g. a host " +
		"libcurl.so picked up at build time"},
	{"uid_map", "the user-namespace id map could not be written"},
}

// tailText quotes the child's own last words for an error message.
func tailText(c *child) string {
	if c == nil {
		return "    (the launcher produced no output)"
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.tail) == 0 {
		return "    (the launcher produced no output)"
	}
	lines := make([]string, 0, len(c.tail))
	for _, line := range c.tail {
		lines = append(lines, "    "+line)
	}
	return strings.Join(lines, "\n")
}

// diagnoseTail names the launcher's failure from its own `perror` strings, where possible.
//
// The strings are the launcher's, and the difference is between "did not become ready" and
// "the kernel refused to create the namespaces, which is what a container without
// `systempaths=unconfined` produces".
func diagnoseTail(c *child, host string, port int) string {
	if c == nil {
		return ""
	}
	c.mu.Lock()
	text := strings.Join(c.tail, "\n")
	c.mu.Unlock()
	if strings.Contains(text, SelfSignal) {
		return " The payload signalled *itself*, which its own code does when " +
			"`svr.listen()` fails (`lite_main.cpp:705`) -- the usual cause is EADDRINUSE on " +
			fmt.Sprintf("%s:%d.", host, port)
	}
	for _, pair := range namespaceFailures {
		if strings.Contains(text, pair.marker) {
			return " That looks like " + pair.explanation + "."
		}
	}
	return ""
}

// drainPump lets the pump finish a child's output, without ever abandoning the reader.
//
// A timeout here means "stop waiting", not "cancel the pump" -- losing the pump is how
// output gets lost.
func (s *Supervisor) drainPump(c *child) {
	if c == nil {
		return
	}
	select {
	case <-c.pumpDone:
	case <-time.After(500 * time.Millisecond):
	}
}

// reap waits for a child that is already on its way out and drains its pump.
func (s *Supervisor) reap(c *child) {
	if c == nil {
		return
	}
	<-c.done
	s.drainPump(c)
}

// terminate SIGTERMs one child on its own pid, then reaps it. R7, in one place.
//
// The pid, never the process group: the launcher `unshare`s `CLONE_NEWPID`, so the payload
// is PID 1 of a nested PID namespace where a group signal does not mean what it appears
// to. Signalling the launcher is also sufficient and graceful -- it forwards to its
// chrooted child, which stops its server via `sigwait`.
func (s *Supervisor) terminate(c *child) {
	if c == nil {
		return
	}
	if !c.alive() {
		s.reap(c)
		return
	}
	s.emit("stopping the %s (pid %d) with SIGTERM", labelOf(c), c.pid)
	_ = c.cmd.Process.Signal(sigterm)
	select {
	case <-c.done:
	case <-time.After(StopTimeout):
		s.emit("the %s did not exit within %.0fs of SIGTERM; SIGKILL to pid %d",
			labelOf(c), StopTimeout.Seconds(), c.pid)
		_ = c.cmd.Process.Signal(sigkill)
		<-c.done
	}
	// After the exit, not before: the child needs someone reading the pipe until it
	// closes, or a full buffer would block its own shutdown.
	s.drainPump(c)
}

// shutdownService tears down the serving child, if any.
func (s *Supervisor) shutdownService() {
	s.mu.Lock()
	child := s.service
	s.service = nil
	s.mu.Unlock()
	s.terminate(child)
}

// shutdownLogin tears down the login child. A login child exits on its own when it is
// done, and its own exit is not a failure.
//
// The 2FA file is removed when the child was still running, i.e. when *we* stopped it.
// That is the case where the child cannot have removed it: removing the file is the last
// thing the login does, and a signal kills it before then. Leaving the file would hand the
// next login a code with no prompt, which appends to whatever password that login is using.
func (s *Supervisor) shutdownLogin() {
	s.mu.Lock()
	child := s.login
	s.login = nil
	s.mu.Unlock()
	if child == nil {
		return
	}
	weSignalled := child.alive()
	s.terminate(child)
	if weSignalled {
		s.discard2FAFile("left behind by a login that was stopped")
	}
}

// sortByLengthDesc orders secrets so the longest is replaced first. Insertion order must
// not decide it (see `scrub`).
func sortByLengthDesc(values []string) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && len(values[j]) > len(values[j-1]); j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}
