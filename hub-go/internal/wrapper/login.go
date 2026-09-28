package wrapper

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// TwoFAFilename is the file `auth.cpp:62` reads the 2FA code from, named once because the
// supervisor has to create it and the child has to remove it.
const TwoFAFilename = "2fa.txt"

// ChildTwoFAWindow is the child's own wait for that file: `while (!file_exists && count <
// 20) sleep(3)` at `auth.cpp:84-88`, then `exit(1)`. A challenge may never promise more
// than this, whatever the supervisor's TTL is set to -- a longer TTL would tell a user they
// have time the wrapper does not have.
const ChildTwoFAWindow = 60 * time.Second

// twoFAMarkers is the 2FA prompt heuristic, copied from `wrapper/gui/main.go:653-657`
// (`check2FA`) rather than reinvented. These are the only five strings the upstream GUI
// treats as a 2FA request, and three come from `wrapper/lite/auth.cpp`'s own output.
var twoFAMarkers = []string{
	"need2FA: true",
	"2FA: true",
	"2FA code",
	"requiresHSA2VerificationCode",
	"Enter your 2FA code into",
}

// Login logs in to Apple Music and waits for the wrapper to ask for a 2FA code.
//
// Runs the launcher a second time, in its `--login` mode, with the credentials on its argv
// -- the only input it takes (`lite_main.cpp:516-518`). It is a separate, short-lived
// process from the serving one: the payload's login mode caches tokens and returns 0, it
// never listens. So this does not need the serving child to be up, which is what makes it
// usable at all: `Start` waits for `regions`, and `regions` stay empty until an account is
// logged in.
//
// Returns the pending challenge once the child asks for a code. Returns an error if it
// never asks within `LoginPromptTimeout` -- either the account needs no 2FA (the login is
// done; check `/status`) or the credentials were rejected (the child's own output says
// which). There is no non-2FA return value to give, because there is no code left to
// submit.
//
// A serving child started before this point will not pick the new account up on its own:
// the payload loads its token cache at start-up. Call `Stop` and `Start` again after a
// successful login.
func (s *Supervisor) Login(ctx context.Context, username, password string) (*Challenge, error) {
	s.mu.Lock()
	adopted := s.adopted
	s.mu.Unlock()
	if adopted {
		return nil, fail("this hub adopted a wrapper that was already running, and an " +
			"adopted wrapper cannot be logged into from here -- its login happens in its " +
			"own process, not this one. Log in on the wrapper side (for example " +
			"`lite --login user:pass`) and retry.")
	}

	// A second login supersedes the first. Outstanding challenges are dropped with it,
	// and deliberately: their login child is gone, and the new one watches the *same*
	// `2fa.txt`, so a code offered against a stale challenge would be picked up by the new
	// login and appended to the wrong account's password (`auth.cpp:92-96` concatenates
	// the code onto the password).
	s.shutdownLogin()
	s.mu.Lock()
	dropped := len(s.challenges)
	s.challenges = map[string]time.Time{}
	s.mu.Unlock()
	if dropped > 0 {
		s.emit("discarding %d outstanding 2FA challenge(s) from the previous login attempt", dropped)
	}
	// A leftover 2fa.txt is removed here, before anything is spawned, because this is the
	// one moment a file at that path is unambiguously not ours: a previous login that was
	// stopped rather than completed cannot remove it (the child only unlinks at
	// `auth.cpp:99`, which SIGTERM prevents), and the wait at `auth.cpp:84` is guarded by
	// `file_exists` while the `fopen`/`fscanf` that follows is *not* -- so a stale file is
	// appended to the new password the instant it appears, with no prompt and no warning.
	s.discard2FAFile("before starting a new login")

	s.rememberSecret(username)
	s.rememberSecret(password)
	// The joined form too: it is exactly what lands in argv and in /proc/<pid>/cmdline,
	// and it is what a child that echoes its own argv would print.
	s.rememberSecret(username + ":" + password)

	argv := []string{
		s.binary,
		"--login", username + ":" + password,
		// Takes the file path unconditionally, rather than leaving it to the isatty test
		// that a piped stdin can never pass (`auth.cpp:64`). Same flag, same reason, as
		// the Go GUI's argv (`main.go:733`).
		"--code-from-file",
		"--base-dir", s.baseDir,
	}
	// Emitted without the credentials even though they are in argv: this line goes to the
	// UI, and the scrub is a second line of defence rather than the only one.
	s.emit("spawning %s in login mode with --code-from-file (--base-dir %s)", s.binary, s.baseDir)

	login, err := s.spawn(argv, "login")
	if err != nil {
		return nil, fail("could not start the wrapper launcher %s to log in: %v", s.binary, err)
	}
	s.mu.Lock()
	s.login = login
	s.mu.Unlock()

	// Wait for whichever comes first: the 2FA prompt, or the child exiting. The login mode
	// exits by itself when it is done, so an account that needs no 2FA is the *common*
	// outcome and must not cost the full prompt timeout before the caller hears about it.
	select {
	case <-login.twofaEvent:
	case <-login.done:
	case <-ctx.Done():
		s.shutdownLogin()
		return nil, ctx.Err()
	case <-time.After(LoginPromptTimeout):
	}

	login.mu.Lock()
	seen := login.twofaSeen
	seenAt := login.twofaSeenAt
	login.mu.Unlock()

	if !seen {
		s.drainPump(login)
		detail := tailText(login)
		s.shutdownLogin()
		// Only worth pointing at a URL when there is one: on a fresh install nothing is
		// serving, and "check http://127.0.0.1:0/status" would be nonsense.
		check := "start the wrapper and check its /status"
		if s.BoundPort() != 0 {
			check = fmt.Sprintf("check %s", s.statusURL(s.BoundPort()))
		}
		if login.alive() {
			return nil, fail("the wrapper did not ask for a 2FA code within %.0fs. If this "+
				"account needs no 2FA the login is done -- %s. Otherwise the credentials "+
				"were rejected, and the wrapper's own output says why:\n%s",
				LoginPromptTimeout.Seconds(), check, detail)
		}
		return nil, fail("the login process finished without asking for a 2FA code (exit "+
			"code %d). If this account needs no 2FA the login is done -- %s. Otherwise it "+
			"was rejected, and the wrapper's own output says why:\n%s",
			login.exitCode(), check, detail)
	}

	// The deadline is the child's, measured from the child's own moment. Minting it from
	// `time.Now()` here would start the clock after the marker, so the supervisor's window
	// would end slightly *later* than the payload's `exit(1)` -- and it is exactly in that
	// sliver that `Submit2FA` can write a file no one will read. The TTL is clamped to
	// `ChildTwoFAWindow` so no configuration can promise more time than the wrapper has.
	window := s.twoFATTL
	if window > ChildTwoFAWindow {
		window = ChildTwoFAWindow
	}
	if seenAt.IsZero() {
		seenAt = time.Now()
	}
	challenge := &Challenge{ID: randomID(), ExpiresAt: seenAt.Add(window)}
	s.mu.Lock()
	s.challenges[challenge.ID] = challenge.ExpiresAt
	s.mu.Unlock()
	remaining := time.Until(challenge.ExpiresAt).Seconds()
	if remaining < 0 {
		remaining = 0
	}
	s.emit("the wrapper asked for a 2FA code; enter it within %.0fs, after which the "+
		"wrapper gives up and exits", remaining)
	return challenge, nil
}

// Submit2FA creates `<rootfs>/<base-dir>/2fa.txt` for the waiting login child, consuming
// the challenge.
//
// A file, not a write to the child's stdin: `auth.cpp:64`'s stdin branch needs a tty and a
// spawned child has a pipe there, so the file is the only channel the payload offers. The
// child polls for it and removes it itself (`auth.cpp:99`) -- but only if it gets that
// far, so `Login` and `shutdownLogin` also remove it.
//
// Single use and TTL-checked: an expired challenge is re-requested, not resent. The code
// joins the redaction set before the file is written, because the child echoes what it
// read back onto its stdout.
func (s *Supervisor) Submit2FA(challengeID, code string) error {
	s.mu.Lock()
	adopted := s.adopted
	expiresAt, known := s.challenges[challengeID]
	login := s.login
	s.mu.Unlock()

	if adopted {
		return fail("this hub adopted a wrapper that was already running, so there is no " +
			"login child here to hand a 2FA code to")
	}
	if !known {
		return fail("unknown or already-used 2FA challenge %q; log in again for a new one",
			challengeID)
	}
	if time.Now().After(expiresAt) {
		s.mu.Lock()
		delete(s.challenges, challengeID)
		s.mu.Unlock()
		return fail("the 2FA challenge %s expired %.0fs ago, around when the wrapper "+
			"stopped waiting for the code; log in again for a new one",
			challengeID, time.Since(expiresAt).Seconds())
	}
	if login == nil || !login.alive() {
		exitText := "nothing, it was never started"
		if login != nil {
			exitText = fmt.Sprintf("%d", login.exitCode())
		}
		return fail("the wrapper's login process is not running, so the 2FA code cannot be "+
			"delivered; it exited with code %s", exitText)
	}

	s.mu.Lock()
	delete(s.challenges, challengeID)
	s.mu.Unlock()
	s.rememberSecret(code)
	path, err := s.write2FAFile(code)
	if err != nil {
		return err
	}
	s.emit("2FA code written to %s; the wrapper picks it up and removes the file", path)
	return nil
}

// twoFAFile is where the login child will look for the code, as a path on *this* side.
//
// `auth.cpp:62` builds the path from `g_base_dir`, which the launcher resolves *after*
// `chdir("./rootfs")` + `chroot(".")` (`wrapper-lite-rootless.c:131-138`) and then mkdirs
// inside that tree -- so on the host it lives under the `rootfs` directory next to the
// binary, whatever the configured base dir says. Verified against the real launcher:
// `--base-dir X` appears as `rootfs/X`.
//
// An absolute `--base-dir` is still chroot-absolute, so the leading separator is dropped
// rather than treated as a host root.
func (s *Supervisor) twoFAFile() (string, error) {
	chroot := filepath.Join(filepath.Dir(s.binary), "rootfs")
	if info, err := os.Stat(chroot); err != nil || !info.IsDir() {
		return "", fail("%s is not a directory, so there is nowhere to put the 2FA file. "+
			"The launcher chroots into ./rootfs relative to its own directory "+
			"(wrapper-lite-rootless.c:131-138) and reads the code from <base-dir>/2fa.txt "+
			"*inside* that tree, so the hub can only hand it a code for a rootfs launcher: "+
			"wrapper-lite-rootless or wrapper-lite. The QEMU launcher (wrapper-lite-qemu) "+
			"has no rootfs at all -- it passes --base-dir into the guest, where the file "+
			"would be invisible from here.", chroot)
	}
	relative := strings.TrimPrefix(s.baseDir, "/")
	return filepath.Join(chroot, relative, TwoFAFilename), nil
}

// discard2FAFile removes the 2FA file if it is there.
//
// This is not tidiness. `auth.cpp:84` guards only the *wait* on `file_exists`; the `fopen`
// + `fscanf` at `:92-96`, which appends whatever the file holds to the password, is
// unconditional. So a leftover file is not a file nobody reads -- the next login consumes
// it instantly, silently, and authenticates with the wrong password appended to it. Called
// from two places, and both are needed: before a new login starts, where a file at that
// path cannot be ours, and after a login child we signalled, where the file is ours and
// orphaned.
func (s *Supervisor) discard2FAFile(why string) bool {
	path, err := s.twoFAFile()
	if err != nil {
		// No rootfs: there is no file to have been left, and `twoFAFile` has already
		// explained itself where it matters.
		return false
	}
	if err := os.Remove(path); err != nil {
		if !os.IsNotExist(err) {
			s.emit("could not remove the leftover 2FA file %s: %v", path, err)
		}
		return false
	}
	s.emit("removed a leftover 2FA file %s %s", path, why)
	return true
}

// write2FAFile creates the 2FA file atomically, owner-only, and returns its path.
//
// Written to a temporary name and renamed into place because the child polls with
// `file_exists` and then `fopen`s: a partially written file would be read as a truncated
// code rather than as no code at all. Mode 0600 because the launcher creates its base dir
// 0777 (`wrapper-lite-rootless.c:142`), so the file's own mode is the only thing keeping
// the code off other readers.
//
// `O_EXCL`, and the temporary name is unlinked first: without both, a `.2fa.txt.<pid>` left
// behind by an earlier run of *this* process keeps whatever mode it had, so the 0600 that
// `O_CREAT` requests is silently not applied.
func (s *Supervisor) write2FAFile(code string) (string, error) {
	path, err := s.twoFAFile()
	if err != nil {
		return "", err
	}
	parent := filepath.Dir(path)
	if info, err := os.Stat(parent); err != nil || !info.IsDir() {
		return "", fail("%s does not exist, so the 2FA code cannot be handed over. The "+
			"wrapper's login process creates it; it may have exited already.", parent)
	}
	temporary := filepath.Join(parent, "."+TwoFAFilename+"."+fmt.Sprintf("%d", os.Getpid()))
	_ = os.Remove(temporary)
	file, err := os.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", fail("could not write the 2FA code to %s: %v", path, err)
	}
	_, writeErr := file.WriteString(code)
	closeErr := file.Close()
	if writeErr == nil {
		writeErr = closeErr
	}
	if writeErr == nil {
		writeErr = os.Rename(temporary, path)
	}
	if writeErr != nil {
		// Covers a crash between `open` and `rename` too, not just a failed open: the
		// temporary is unlinked on every path out of this block.
		_ = os.Remove(temporary)
		return "", fail("could not write the 2FA code to %s: %v", path, writeErr)
	}
	return path, nil
}

// randomID is a challenge id: 32 lowercase hex characters, as `uuid.uuid4().hex` produced.
func randomID() string {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		// crypto/rand failing is a broken system rather than a recoverable condition, and
		// the only consumer is an id a user never types. Time keeps it unique enough.
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buffer)
}
