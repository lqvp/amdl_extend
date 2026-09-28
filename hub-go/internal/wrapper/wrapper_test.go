package wrapper

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// The supervisor has no unit tests for its process choreography here -- that is exercised
// against the real launcher by the deployment -- but the parts a port can silently get
// wrong are pinned below: the scrub order, the backoff curve, where the 2FA file lands,
// how it is written, and how a `/status` envelope is read. Each of these failed a real
// user once.

func TestScrubReplacesTheLongestSecretFirst(t *testing.T) {
	// A username that is a prefix of its own password: replace the short one first and
	// the log keeps `***_PASS` -- a password suffix is still a password.
	supervisor := New(Config{})
	supervisor.rememberSecret("SECRET")
	supervisor.rememberSecret("SECRET_PASS")

	got := supervisor.scrub("user=SECRET pass=SECRET_PASS")
	if got != "user=*** pass=***" {
		t.Fatalf("scrub left a credential behind: %q", got)
	}
}

func TestBackoffIsExponentialAndCapped(t *testing.T) {
	for _, row := range []struct {
		attempt int
		want    time.Duration
	}{
		{1, 500 * time.Millisecond},
		{2, time.Second},
		{3, 2 * time.Second},
		{4, 4 * time.Second},
		{5, 8 * time.Second},
		{6, 8 * time.Second}, // the cap
	} {
		if got := backoff(row.attempt); got != row.want {
			t.Errorf("backoff(%d) = %s, want %s", row.attempt, got, row.want)
		}
	}
}

func testBinary(t *testing.T, withRootfs bool) string {
	t.Helper()
	directory := t.TempDir()
	binary := filepath.Join(directory, "wrapper-lite-rootless")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write binary: %v", err)
	}
	if withRootfs {
		if err := os.MkdirAll(filepath.Join(directory, "rootfs"), 0o755); err != nil {
			t.Fatalf("mkdir rootfs: %v", err)
		}
	}
	return binary
}

func TestThe2FAFileLandsUnderRootfsTheWayTheLauncherChroots(t *testing.T) {
	// `auth.cpp` reads `<base-dir>/2fa.txt` *inside* the chroot the launcher makes from
	// `./rootfs` next to itself, so the host path is `<binary's dir>/rootfs/<base-dir>`.
	// Verified against the real launcher: `--base-dir X` appears as `rootfs/X`.
	binary := testBinary(t, true)
	supervisor := New(Config{Binary: binary, BaseDir: "/data/wrapper"})

	path, err := supervisor.twoFAFile()
	if err != nil {
		t.Fatalf("twoFAFile: %v", err)
	}
	want := filepath.Join(filepath.Dir(binary), "rootfs", "data", "wrapper", TwoFAFilename)
	if path != want {
		t.Fatalf("twoFAFile = %q, want %q", path, want)
	}
}

func TestA2FAFileCannotBeWrittenForTheQEMULauncher(t *testing.T) {
	// The QEMU launcher has no rootfs: `--base-dir` goes into the guest and a file written
	// here would be invisible. Refusing with the explanation is the whole behaviour.
	binary := testBinary(t, false)
	supervisor := New(Config{Binary: binary, BaseDir: "/data/wrapper"})

	_, err := supervisor.twoFAFile()
	if err == nil {
		t.Fatal("expected a refusal for a launcher with no rootfs")
	}
	if !strings.Contains(err.Error(), "wrapper-lite-qemu") {
		t.Fatalf("the refusal must name the launcher that cannot work: %v", err)
	}
}

func TestWrite2FAFileIsAtomicOwnerOnlyAndCleansUp(t *testing.T) {
	binary := testBinary(t, true)
	supervisor := New(Config{Binary: binary, BaseDir: "/data/wrapper"})
	// The login child creates its base dir; this test stands in for it.
	base := filepath.Join(filepath.Dir(binary), "rootfs", "data", "wrapper")
	if err := os.MkdirAll(base, 0o777); err != nil {
		t.Fatalf("mkdir base: %v", err)
	}

	path, err := supervisor.write2FAFile("123456")
	if err != nil {
		t.Fatalf("write2FAFile: %v", err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(body) != "123456" {
		t.Fatalf("the file holds %q, want the bare code", body)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if mode := info.Mode().Perm(); mode != 0o600 {
		t.Fatalf("the code file is mode %#o, want 0600 -- the base dir is 0777, so this "+
			"mode is the only thing keeping the code off other readers", mode)
	}
	// The temporary must not survive: the child would find whichever file appears first.
	leftovers, err := filepath.Glob(filepath.Join(base, ".*"))
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	if len(leftovers) != 0 {
		t.Fatalf("write left temporaries behind: %v", leftovers)
	}
}

func TestTheStatusEnvelopeIsReadTheWayTheDownloaderReadsIt(t *testing.T) {
	// The hub and `AppleMusicDecrypt/src/wrapper.py` must agree on what a healthy instance
	// looks like, or the same wrapper reads as up in one and down in the other.
	cases := []struct {
		name    string
		body    string
		status  int
		usable  bool
		regions int
		detail  string
	}{
		{"healthy", `{"code": 0, "msg": "ok", "data": {"regions": ["jp"]}}`, 200, true, 1, "HTTP 200, code=0"},
		{"no account", `{"code": 0, "msg": "ok", "data": {"regions": []}}`, 200, true, 0, "HTTP 200, code=0"},
		{"error code", `{"code": 5, "msg": "denied", "data": {}}`, 200, false, 0, "code=5"},
		{"not json", `hello`, 200, false, 0, "non-JSON"},
		{"not the envelope", `{"hello": 1}`, 200, false, 0, "unexpected envelope"},
		{"http error", `nope`, 502, false, 0, "HTTP 502"},
	}
	for _, row := range cases {
		t.Run(row.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(row.status)
				_, _ = w.Write([]byte(row.body))
			}))
			defer server.Close()

			supervisor := New(Config{Host: "127.0.0.1"})
			url := strings.TrimPrefix(server.URL, "http://")
			host, portText, _ := strings.Cut(url, ":")
			port, err := strconv.Atoi(portText)
			if err != nil {
				t.Fatalf("port: %v", err)
			}
			supervisor.host = host

			result := supervisor.probeStatus(context.Background(), port)
			if result.usable != row.usable {
				t.Fatalf("usable = %v, want %v (%s)", result.usable, row.usable, result.detail)
			}
			if len(result.regions) != row.regions {
				t.Fatalf("regions = %v, want %d entries", result.regions, row.regions)
			}
			if !strings.Contains(result.detail, row.detail) {
				t.Fatalf("detail %q does not name %q", result.detail, row.detail)
			}
		})
	}
}

func TestTheRefusalNamesTheSelfSignalWhenAPortIsStolen(t *testing.T) {
	// The port pre-flight exists because a bind failure makes the payload log
	// `received signal 15` -- indistinguishable from an external kill -- so the message
	// that replaces it has to name that line.
	if !strings.Contains(SelfSignal, "received signal 15") {
		t.Fatalf("SelfSignal = %q; it is quoted in the port-in-use message", SelfSignal)
	}
}
