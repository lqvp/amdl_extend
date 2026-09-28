package normalize_test

import (
	"testing"

	"amdhub/internal/normalize"
	"amdhub/internal/testutil"
	"amdhub/internal/ucd"
)

// goldenName is one row of `testdata/golden/normalize.json`, every field of which
// was produced by `hub.normalize` and `unicodedata` in this repository's own
// Python environment.
type goldenName struct {
	Input                string `json:"input"`
	Normalized           string `json:"normalized"`
	NormalizedKeepPrefix string `json:"normalized_keep_prefix"`
	AlbumKey             string `json:"album_key"`
	IsAudioFile          bool   `json:"is_audio_file"`
	Stem                 string `json:"stem"`
	NFKC                 string `json:"nfkc"`
	CaseFold             string `json:"casefold"`
}

type goldenNormalize struct {
	Names []goldenName `json:"names"`
}

func loadGoldenNames(t *testing.T) []goldenName {
	t.Helper()
	var golden goldenNormalize
	testutil.LoadGolden(t, "normalize.json", &golden)
	if len(golden.Names) < 1000 {
		// A truncated corpus would make this test pass for the wrong reason, and
		// the generator is a separate program that can be run with a stale tree.
		t.Fatalf("golden corpus has %d names, expected the generated corpus", len(golden.Names))
	}
	return golden.Names
}

// TestNFKCMatchesPython is the port's strongest claim about Unicode: this
// package's NFKC is CPython's NFKC, over an input set that includes every
// character class that decides the difference (fullwidth forms, compatibility
// decompositions, Hangul, combining marks, canonical singletons, composition
// exclusions).
func TestNFKCMatchesPython(t *testing.T) {
	for _, row := range loadGoldenNames(t) {
		if got := ucd.NFKC(row.Input); got != row.NFKC {
			t.Errorf("ucd.NFKC(%q) = %q, Python NFKC = %q", row.Input, got, row.NFKC)
		}
	}
}

func TestCaseFoldMatchesPython(t *testing.T) {
	for _, row := range loadGoldenNames(t) {
		if got := ucd.CaseFold(row.NFKC); got != row.CaseFold {
			t.Errorf("ucd.CaseFold(%q) = %q, Python casefold = %q", row.NFKC, got, row.CaseFold)
		}
	}
}

// TestNormalizeMatchesPython is the whole point of the port: a Go hub that folded
// a name differently from the Python hub it replaced would re-download -- or,
// worse, falsely skip -- files the other one had already indexed, and it would do
// it silently.
func TestNormalizeMatchesPython(t *testing.T) {
	for _, row := range loadGoldenNames(t) {
		if got := normalize.Normalize(row.Input, true); got != row.Normalized {
			t.Errorf("Normalize(%q, true) = %q, want %q", row.Input, got, row.Normalized)
		}
		if got := normalize.Normalize(row.Input, false); got != row.NormalizedKeepPrefix {
			t.Errorf("Normalize(%q, false) = %q, want %q", row.Input, got, row.NormalizedKeepPrefix)
		}
		// `AlbumKey` is `Normalize(..., false)` reached through the function the
		// index and the lookup both use, so a drift between the two would make
		// every album lookup miss.
		if got := normalize.AlbumKey(row.Input); got != row.AlbumKey || got != row.NormalizedKeepPrefix {
			t.Errorf("AlbumKey(%q) = %q, want %q", row.Input, got, row.AlbumKey)
		}
		if got := normalize.IsAudioFile(row.Input); got != row.IsAudioFile {
			t.Errorf("IsAudioFile(%q) = %v, want %v", row.Input, got, row.IsAudioFile)
		}
		if got := normalize.StemOf(row.Input); got != row.Stem {
			t.Errorf("StemOf(%q) = %q, want %q", row.Input, got, row.Stem)
		}
	}
}

// TestStemOfNeverSplitsOnSlash is the fullwidth-solidus rule, asserted on its own
// because it is the one that is silent if it breaks: "01. A／B.m4a" collapsing to
// "b" would collide with "01. B.m4a" and falsely skip a track.
func TestStemOfNeverSplitsOnSlash(t *testing.T) {
	const name = "01. A／B.m4a"
	if got := normalize.StemOf(name); got != "01. A／B" {
		t.Fatalf("StemOf(%q) = %q, want %q", name, got, "01. A／B")
	}
	if got := normalize.Normalize(name, true); got != "a/b" {
		t.Fatalf("Normalize(%q, true) = %q, want %q", name, got, "a/b")
	}
}

// TestEmptyKeyMeansUnusable pins the value a caller must refuse to match on.
// `normalize` reports the fact; the guard that acts on it lives in `dedup`.
func TestEmptyKeyMeansUnusable(t *testing.T) {
	for _, name := range []string{"", " ", "　", "!", "!!!", "。", "...", "-", "_", "\u001c"} {
		if got := normalize.Normalize(name, true); got != "" {
			t.Errorf("Normalize(%q, true) = %q, want the empty key", name, got)
		}
	}
	for _, name := range []string{"4pi", "1st EP", "1979 - Song", "Ⅻ", "A"} {
		if got := normalize.Normalize(name, true); got == "" {
			t.Errorf("Normalize(%q, true) = \"\", want a usable key", name)
		}
	}
}
