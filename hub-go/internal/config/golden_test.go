package config_test

import (
	"testing"

	"amdhub/internal/config"
	"amdhub/internal/testutil"
)

type goldenSettings struct {
	Password         string   `json:"password"`
	Bind             string   `json:"bind"`
	Port             int      `json:"port"`
	LibraryRoots     []string `json:"library_roots"`
	RipConcurrency   int      `json:"rip_concurrency"`
	WrapperBinary    string   `json:"wrapper_binary"`
	WrapperBaseDir   string   `json:"wrapper_base_dir"`
	WrapperHost      string   `json:"wrapper_host"`
	WrapperPort      int      `json:"wrapper_port"`
	DedupArtistScope string   `json:"dedup_artist_scope"`
	DBPath           string   `json:"db_path"`
	SessionSecretLen int      `json:"session_secret_len"`
	SessionSecret    *string  `json:"session_secret"`
}

type goldenConfigCase struct {
	Name     string          `json:"name"`
	Env      map[string]string `json:"env"`
	Error    string          `json:"error"`
	Settings *goldenSettings `json:"settings"`
}

type goldenConfig struct {
	Cases []goldenConfigCase `json:"cases"`
}

// TestLoadMatchesPython checks the port against `hub.config.load_settings` on
// every case the Python suite cares about: the defaults, a fully specified
// environment, blank values falling back, and each failure mode with the message
// an operator is told to look for in `.env.example`.
//
// The messages are compared *verbatim*, which is why the port carries `pyRepr`.
// A deployment's failure text is the only thing standing between a typo and a
// silent fallback, and "close enough" quoting would change what a user greps for.
func TestLoadMatchesPython(t *testing.T) {
	var golden goldenConfig
	testutil.LoadGolden(t, "config.json", &golden)
	if len(golden.Cases) == 0 {
		t.Fatal("no cases in the golden corpus")
	}

	for _, want := range golden.Cases {
		got, err := config.Load(want.Env)
		if want.Error != "" {
			if err == nil {
				t.Errorf("%s: Load succeeded, Python refused with %q", want.Name, want.Error)
				continue
			}
			if err.Error() != want.Error {
				t.Errorf("%s: error = %q, want %q", want.Name, err.Error(), want.Error)
			}
			continue
		}
		if err != nil {
			t.Errorf("%s: Load failed: %v", want.Name, err)
			continue
		}
		if got.Password != want.Settings.Password ||
			got.Bind != want.Settings.Bind ||
			got.Port != want.Settings.Port ||
			got.RipConcurrency != want.Settings.RipConcurrency ||
			got.WrapperBinary != want.Settings.WrapperBinary ||
			got.WrapperBaseDir != want.Settings.WrapperBaseDir ||
			got.WrapperHost != want.Settings.WrapperHost ||
			got.WrapperPort != want.Settings.WrapperPort ||
			got.DedupArtistScope != want.Settings.DedupArtistScope ||
			got.DBPath != want.Settings.DBPath {
			t.Errorf("%s: settings = %+v, want %+v", want.Name, got, want.Settings)
		}
		if len(got.LibraryRoots) != len(want.Settings.LibraryRoots) {
			t.Errorf("%s: library roots = %v, want %v", want.Name, got.LibraryRoots, want.Settings.LibraryRoots)
		} else {
			for i := range got.LibraryRoots {
				if got.LibraryRoots[i] != want.Settings.LibraryRoots[i] {
					t.Errorf("%s: library roots = %v, want %v", want.Name, got.LibraryRoots, want.Settings.LibraryRoots)
					break
				}
			}
		}
		if len(got.SessionSecret) != want.Settings.SessionSecretLen {
			t.Errorf("%s: session secret length = %d, want %d",
				want.Name, len(got.SessionSecret), want.Settings.SessionSecretLen)
		}
		if want.Settings.SessionSecret != nil && string(got.SessionSecret) != *want.Settings.SessionSecret {
			t.Errorf("%s: session secret = %q, want %q",
				want.Name, got.SessionSecret, *want.Settings.SessionSecret)
		}
	}
}

// TestPasswordIsCheckedBeforeLibraryRoots pins the order of the two required
// variables: a caller who has set neither is told about the password, which is
// the one it is more likely to have meant.
func TestPasswordIsCheckedBeforeLibraryRoots(t *testing.T) {
	_, err := config.Load(map[string]string{})
	if err == nil {
		t.Fatal("Load with an empty environment succeeded")
	}
	if got := err.Error(); got[:len("AMD_PASSWORD")] != "AMD_PASSWORD" {
		t.Fatalf("error = %q, want it to name AMD_PASSWORD first", got)
	}
}

// TestSessionSecretIsGeneratedPerProcess checks the one field that is random by
// design: a missing secret must not degrade into a constant that ships in the
// image.
func TestSessionSecretIsGeneratedPerProcess(t *testing.T) {
	env := map[string]string{"AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library"}
	first, err := config.Load(env)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	second, err := config.Load(env)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(first.SessionSecret) != config.MinSessionSecretChars {
		t.Fatalf("secret length = %d, want %d", len(first.SessionSecret), config.MinSessionSecretChars)
	}
	if string(first.SessionSecret) == string(second.SessionSecret) {
		t.Fatal("two loads generated the same secret")
	}
}

// TestSecretLengthIsCountedInCharacters is the port's one deliberate difference
// from a naive translation: Python's `len(raw)` counts code points, and a
// 32-character Japanese passphrase is 96 bytes.
func TestSecretLengthIsCountedInCharacters(t *testing.T) {
	secret := "秘密の合言葉秘密の合言葉秘密の合言葉秘密の合言葉" // 24 characters
	if _, err := config.Load(map[string]string{
		"AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_SESSION_SECRET": secret,
	}); err == nil {
		t.Fatal("a 24-character secret was accepted")
	}
	if _, err := config.Load(map[string]string{
		"AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library",
		"AMD_SESSION_SECRET": secret + "秘密の合言葉秘密の合言葉",
	}); err != nil {
		t.Fatalf("a 36-character secret was refused: %v", err)
	}
}
