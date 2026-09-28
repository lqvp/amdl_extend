// Package config is a port of `hub/config.py`: environment -> `Settings`.
//
// Parsing is kept explicit rather than declarative: each value has a documented
// default and a documented failure mode, and a misconfigured deployment must fail
// at startup with a message naming the variable -- not at the first request, and
// not by silently falling back to a different mode.
//
// **Every error message is the Python one, down to the quotes**, because the
// deployment's failure modes are documented in `README.md` and `.env.example` in
// those words, and because `TestLoadMatchesPython` compares them. The quoting is
// Python's `repr`, which is not Go's `%q`: see `pyRepr`.
package config

import (
	"crypto/rand"
	"fmt"
	"os"
	"strconv"
	"strings"
	"unicode/utf8"

	"amdhub/internal/library"
	"amdhub/internal/pyrepr"
)

// The defaults, each of which is a decision the Python module documents.
const (
	DefaultBind = "0.0.0.0" // Reachable from the LAN.
	DefaultPort = 8080
	// The wrapper is loopback-only and its port must never be published.
	DefaultWrapperHost   = "127.0.0.1"
	DefaultWrapperPort   = 12340
	DefaultWrapperBinary = "/usr/local/bin/wrapper-lite-qemu"
	DefaultWrapperBaseDir = "/data/wrapper"
	// Only job state is persisted, so the hub needs exactly one database file,
	// and it lives on the hub-data volume.
	DefaultDBPath = "/data/hub.db"

	// DefaultRipConcurrency is how many tracks to rip at once, and why four.
	// Measured rather than assumed: a 41.8 MB ALAC track takes 9.8 s, and 6.1 s
	// of that is the wrapper answering `/lyrics`, the album lookup and the codec
	// check before a byte of audio moves; the audio then crosses at ~41 MB/s in
	// about a second. So ~85% of a track's wall clock is an API round-trip, and a
	// serial queue spent almost all of it waiting for a sibling that was not
	// running. Four overlapping rips recover most of that, and it is a number to
	// put on a home connection without worrying the Apple account -- upstream's
	// own ceiling (`maxRunningTasks`, 128) is tuned for a TUI driven by a person.
	DefaultRipConcurrency = 4

	MinSessionSecretChars = 32
	// A port outside this range cannot be bound, and the failure would otherwise
	// surface as an error from the server at startup rather than as a named
	// misconfiguration.
	MinPort = 1
	MaxPort = 65535
)

// Settings is the validated configuration. Every field is supplied by `Load`.
type Settings struct {
	Password     string
	Bind         string
	Port         int
	LibraryRoots []string
	// WrapperBinary is the launcher the supervisor starts. The image ships
	// `wrapper-lite-rootless`, not `wrapper-lite-qemu`: the QEMU build has no host
	// rootfs and cannot serve the 2FA code the hub writes.
	WrapperBinary string
	WrapperBaseDir string
	WrapperHost   string
	WrapperPort   int
	// RipConcurrency is how many tracks to rip at once. Upstream's
	// `DownloadManager` has been built for concurrency all along --
	// `asyncio.Semaphore(maxRunningTasks)`, 128 by default -- and only the hub
	// serialised. The port keeps the number, and keeps the reason it is not
	// raised: 85% of a track's wall clock is the wrapper's metadata round-trip,
	// so overlapping is nearly free, and four is a number to put on a home
	// connection without worrying about the account.
	RipConcurrency   int
	DedupArtistScope string
	DBPath           string
	SessionSecret    []byte
}

// Load builds `Settings` from `env`, or from the process environment when `env`
// is nil.
//
// Every failure is an error naming the variable, and never a validation error
// raised from a struct tag: an operator fixes this in the environment, and the
// message is the only tool they have.
func Load(env map[string]string) (*Settings, error) {
	source := env
	if source == nil {
		source = environ()
	}

	// Single shared password, no default, never hardcoded.
	password := strings.TrimSpace(source["AMD_PASSWORD"])
	if password == "" {
		return nil, fmt.Errorf("AMD_PASSWORD is unset or empty. The hub has one shared " +
			"password and no default; set it in the environment (compose reads it from .env).")
	}

	// Both of these are required, and the order matters: a caller who has set
	// neither should be told about the password, which is the one it is more
	// likely to have meant. `TestPasswordIsCheckedBeforeLibraryRoots` pins this.
	libraryRoots, err := requiredPaths(source, "AMD_LIBRARY_ROOTS", "/library")
	if err != nil {
		return nil, err
	}

	port, err := portOf(source, "AMD_PORT", DefaultPort)
	if err != nil {
		return nil, err
	}
	wrapperPort, err := portOf(source, "AMD_WRAPPER_PORT", DefaultWrapperPort)
	if err != nil {
		return nil, err
	}
	concurrency, err := concurrency(source)
	if err != nil {
		return nil, err
	}
	scope, err := artistScope(source)
	if err != nil {
		return nil, err
	}
	secret, err := sessionSecret(source)
	if err != nil {
		return nil, err
	}

	return &Settings{
		Password:       password,
		Bind:           text(source, "AMD_BIND", DefaultBind),
		Port:           port,
		LibraryRoots:   libraryRoots,
		RipConcurrency: concurrency,
		WrapperBinary: library.CleanPath(
			text(source, "AMD_WRAPPER_BINARY", DefaultWrapperBinary)),
		WrapperBaseDir: library.CleanPath(
			text(source, "AMD_WRAPPER_BASE_DIR", DefaultWrapperBaseDir)),
		WrapperHost:      text(source, "AMD_WRAPPER_HOST", DefaultWrapperHost),
		WrapperPort:      wrapperPort,
		DedupArtistScope: scope,
		DBPath:           library.CleanPath(text(source, "AMD_DB_PATH", DefaultDBPath)),
		SessionSecret:    secret,
	}, nil
}

func environ() map[string]string {
	out := map[string]string{}
	for _, pair := range os.Environ() {
		key, value, _ := strings.Cut(pair, "=")
		out[key] = value
	}
	return out
}

// text returns an unset, empty, or whitespace-only variable as the default.
func text(env map[string]string, key, fallback string) string {
	value := env[key]
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return strings.TrimSpace(value)
}

func portOf(env map[string]string, key string, fallback int) (int, error) {
	raw := text(env, key, "")
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer, got %s", key, pyrepr.Str(raw))
	}
	if value < MinPort || value > MaxPort {
		return 0, fmt.Errorf("%s must be between %d and %d, got %d", key, MinPort, MaxPort, value)
	}
	return value, nil
}

// paths parses a comma-separated list, dropping blanks so a trailing comma is not
// a path of "".
func paths(env map[string]string, key string, fallback []string) []string {
	raw := text(env, key, "")
	if raw == "" {
		return fallback
	}
	return splitPaths(raw)
}

// splitPaths is the parsing itself, taking a value already known to be set.
//
// Split out from `paths` because "unset" and "set but unusable" are different
// failures with different messages, and a single function that answered an empty
// slice for both could not tell them apart: `AMD_LIBRARY_ROOTS=" , , "` is set, and
// an operator who has set it must be told that what they set contains no path
// rather than that they forgot it.
func splitPaths(raw string) []string {
	var roots []string
	for _, part := range strings.Split(raw, ",") {
		if strings.TrimSpace(part) == "" {
			continue
		}
		roots = append(roots, library.CleanPath(strings.TrimSpace(part)))
	}
	return roots
}

// requiredPaths is the same parsing as `paths`, with no fallback.
//
// A default for one of these is a host-specific path baked into the source, and
// it is wrong on every machine but the one it was written on. The message carries
// `example` so the operator can see the format rather than infer it -- and the
// noun has to say the example is a *container-side* path: an earlier wording said
// "host directory" while showing `/library`, and an operator who obeys the noun
// sets the host path, which resolves to nothing inside the container.
func requiredPaths(env map[string]string, key, example string) ([]string, error) {
	raw := text(env, key, "")
	if raw == "" {
		return nil, fmt.Errorf("%s is unset. Name every container-side directory that holds "+
			"your music library, comma-separated, e.g. %s=%s. If you meant a directory on this "+
			"host, that is a different variable (see the deployment's .env).", key, key, example)
	}
	roots := splitPaths(raw)
	if len(roots) == 0 {
		return nil, fmt.Errorf("%s is set but contains no usable path", key)
	}
	return roots, nil
}

func artistScope(env map[string]string) (string, error) {
	value := text(env, "AMD_DEDUP_ARTIST_SCOPE", "loose")
	// Not a plain default lookup: an unrecognised value is a typo, and silently
	// degrading to "loose" would re-enable the false-skip mode the closed set
	// exists to warn about.
	if value == "loose" || value == "strict" {
		return value, nil
	}
	return "", fmt.Errorf("AMD_DEDUP_ARTIST_SCOPE must be 'loose' or 'strict', got %s", pyrepr.Str(value))
}

func concurrency(env map[string]string) (int, error) {
	// The constant rather than the number written twice: a default that lives in
	// both the field and the reader is two values to keep in step, and the one
	// that loses is the one nobody reads.
	raw := text(env, "AMD_RIP_CONCURRENCY", "")
	if raw == "" {
		return DefaultRipConcurrency, nil
	}
	count, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("AMD_RIP_CONCURRENCY must be a whole number, got %s", pyrepr.Str(raw))
	}
	// One is not an error -- it is the old behaviour, and someone may want it to
	// compare -- but zero and negatives are, because a gather over an empty or
	// negative list would claim work it never started and leave the jobs `running`
	// for ever.
	if count < 1 {
		return 0, fmt.Errorf("AMD_RIP_CONCURRENCY must be at least 1, got %d. A value of 1 "+
			"is the previous one-track-at-a-time behaviour.", count)
	}
	return count, nil
}

func sessionSecret(env map[string]string) ([]byte, error) {
	raw := strings.TrimSpace(env["AMD_SESSION_SECRET"])
	if raw == "" {
		// Generated per process on purpose. A missing secret must not degrade
		// into a constant that ships in the image, and regenerating costs only
		// the user's own logged-in sessions across a restart. Set
		// AMD_SESSION_SECRET to keep them.
		secret := make([]byte, MinSessionSecretChars)
		if _, err := rand.Read(secret); err != nil {
			return nil, fmt.Errorf("could not generate a session secret: %w", err)
		}
		return secret, nil
	}
	// Characters, not bytes: the Python check is `len(raw)` on a str, and a
	// 32-character Japanese passphrase is 96 bytes and must be accepted exactly
	// as it is here.
	if utf8.RuneCountInString(raw) < MinSessionSecretChars {
		return nil, fmt.Errorf("AMD_SESSION_SECRET must be at least %d characters, got %d; "+
			"a short secret makes the session cookie forgeable",
			MinSessionSecretChars, utf8.RuneCountInString(raw))
	}
	return []byte(raw), nil
}
