package dedup_test

import (
	"reflect"
	"testing"

	"amdhub/internal/dedup"
	"amdhub/internal/library"
	"amdhub/internal/testutil"
)

type goldenCase struct {
	AlbumName   string   `json:"album_name"`
	TrackTitle  string   `json:"track_title"`
	ArtistName  *string  `json:"artist_name"`
	ArtistScope string   `json:"artist_scope"`
	Matched     []string `json:"matched"`
	Resolved    []string `json:"resolved"`
}

type goldenDedup struct {
	Roots []string     `json:"roots"`
	Cases []goldenCase `json:"cases"`
}

// TestFindDuplicateMatchesPython checks every documented decision of the Python
// module through its observable answer: which directories a hit names, which
// album's group they came from, and the two refusals (an unusable key, and
// `strict` with no artist to vouch for the match).
func TestFindDuplicateMatchesPython(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	var golden goldenDedup
	testutil.LoadGolden(t, "dedup.json", &golden)

	scan := library.ScanRoots(golden.Roots)
	for _, want := range golden.Cases {
		got, err := dedup.FindDuplicate(scan, want.AlbumName, want.TrackTitle,
			deref(want.ArtistName), want.ArtistScope)
		if err != nil {
			t.Fatalf("FindDuplicate(%q, %q, %v, %q): %v",
				want.AlbumName, want.TrackTitle, want.ArtistName, want.ArtistScope, err)
		}
		if want.Matched == nil {
			if got != nil {
				t.Errorf("FindDuplicate(%q, %q, %v, %q) = %+v, Python found nothing",
					want.AlbumName, want.TrackTitle, want.ArtistName, want.ArtistScope, got)
			}
			continue
		}
		if got == nil {
			t.Errorf("FindDuplicate(%q, %q, %v, %q) = nil, Python found %v",
				want.AlbumName, want.TrackTitle, want.ArtistName, want.ArtistScope, want.Matched)
			continue
		}
		if !reflect.DeepEqual(got.Matched, want.Matched) {
			t.Errorf("matched = %v, want %v", got.Matched, want.Matched)
		}
		if !reflect.DeepEqual(got.Resolved, want.Resolved) {
			t.Errorf("resolved = %v, want %v", got.Resolved, want.Resolved)
		}
	}
}

// TestUnusableKeyNeverSkips is the unsafe direction, asserted on its own. A false
// skip is unrecoverable -- the track is simply never downloaded -- so it gets its
// own test rather than a row in the table above.
func TestUnusableKeyNeverSkips(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	scan := library.ScanRoots([]string{"testdata/synth/lib1", "testdata/synth/lib2"})
	// The fixture's `薄塩指数/!_` album holds punctuation-only names, so its own
	// track keys contain "". A request whose title folds to "" must not find it.
	for _, title := range []string{"", " ", "!", "!!!", "...", "。", "-"} {
		hit, err := dedup.FindDuplicate(scan, "!_", title, "", dedup.ScopeLoose)
		if err != nil {
			t.Fatalf("FindDuplicate(!_, %q): %v", title, err)
		}
		if hit != nil {
			t.Errorf("FindDuplicate(!_, %q) matched %v; an unusable key must never skip",
				title, hit.Matched)
		}
	}
	// The album side of the same rule: an unusable *album* name cannot be looked
	// up either, even though the index really does hold an empty key.
	hit, err := dedup.FindDuplicate(scan, "!", "Track", "", dedup.ScopeLoose)
	if err != nil {
		t.Fatalf("FindDuplicate: %v", err)
	}
	if hit != nil {
		t.Errorf("an unusable album name matched %v", hit.Matched)
	}
}

func TestUnknownScopeIsAnError(t *testing.T) {
	scan := library.ScanRoots(nil)
	for _, scope := range []string{"", "LOOSE", "loosey", "strict "} {
		if _, err := dedup.FindDuplicate(scan, "Album", "Title", "", scope); err == nil {
			t.Errorf("FindDuplicate with scope %q returned no error", scope)
		}
	}
	for _, scope := range []string{dedup.ScopeLoose, dedup.ScopeStrict} {
		if _, err := dedup.FindDuplicate(scan, "Album", "Title", "", scope); err != nil {
			t.Errorf("FindDuplicate with scope %q: %v", scope, err)
		}
	}
}

// TestStrictNeedsAnArtistToVouch is the documented trade asserted directly: the
// first duplicate shape (one release filed in two places) has an artist level and
// `strict` finds it, the second (a collab fanned out) does not and `strict`
// misses it while `loose` finds it.
func TestStrictNeedsAnArtistToVouch(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	scan := library.ScanRoots([]string{"testdata/synth/lib1", "testdata/synth/lib2"})

	loose, err := dedup.FindDuplicate(scan, "x", "Track", "9Lana", dedup.ScopeLoose)
	if err != nil || loose == nil {
		t.Fatalf("loose match over two roots: hit=%v err=%v", loose, err)
	}
	if len(loose.Matched) != 2 {
		t.Fatalf("loose matched %v, want both roots' copies", loose.Matched)
	}
	// The two copies are indistinguishable in `matched` -- identical relpaths --
	// and distinguishable in `resolved`, which is why the second field exists.
	if loose.Matched[0] != loose.Matched[1] {
		t.Fatalf("expected identical relpaths, got %v", loose.Matched)
	}
	if loose.Resolved[0] == loose.Resolved[1] {
		t.Fatalf("resolved collapsed the two copies: %v", loose.Resolved)
	}

	strict, err := dedup.FindDuplicate(scan, "x", "Track", "9Lana", dedup.ScopeStrict)
	if err != nil || strict == nil || len(strict.Matched) != 2 {
		t.Fatalf("strict match with the right artist: hit=%+v err=%v", strict, err)
	}
	missed, err := dedup.FindDuplicate(scan, "x", "Track", "Someone Else", dedup.ScopeStrict)
	if err != nil {
		t.Fatalf("strict with a different artist: %v", err)
	}
	if missed != nil {
		t.Fatalf("strict matched an album under a different artist: %v", missed.Matched)
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
