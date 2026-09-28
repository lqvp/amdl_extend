package library_test

import (
	"reflect"
	"testing"

	"amdhub/internal/library"
	"amdhub/internal/testutil"
)

type goldenAlbum struct {
	RootIndex int      `json:"root_index"`
	Relpath   string   `json:"relpath"`
	Name      string   `json:"name"`
	Artist    *string  `json:"artist"`
	TrackKeys []string `json:"track_keys"`
	Resolved  string   `json:"resolved"`
}

type goldenScan struct {
	Roots     []string            `json:"roots"`
	Reachable []bool              `json:"reachable"`
	Degraded  []string            `json:"degraded"`
	PerRoot   []int               `json:"per_root"`
	Albums    []goldenAlbum       `json:"albums"`
	ByName    map[string][]string `json:"by_name"`
}

func TestScanMatchesPython(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	var golden goldenScan
	testutil.LoadGolden(t, "scan.json", &golden)

	scan := library.ScanRoots(golden.Roots)

	if !reflect.DeepEqual(scan.Roots, golden.Roots) {
		t.Fatalf("roots = %q, want %q", scan.Roots, golden.Roots)
	}
	if !reflect.DeepEqual(scan.Reachable, golden.Reachable) {
		t.Errorf("reachable = %v, want %v", scan.Reachable, golden.Reachable)
	}
	if !reflect.DeepEqual(scan.Degraded(), golden.Degraded) {
		t.Errorf("degraded = %v, want %v", scan.Degraded(), golden.Degraded)
	}
	if !reflect.DeepEqual(scan.PerRoot(), golden.PerRoot) {
		t.Errorf("perRoot = %v, want %v", scan.PerRoot(), golden.PerRoot)
	}
	if len(scan.Albums) != len(golden.Albums) {
		t.Fatalf("found %d albums, Python found %d", len(scan.Albums), len(golden.Albums))
	}
	// Order as well as contents: relpaths are shown to the user in `skip_reason`
	// and the walk order is what makes two scans of one unchanged tree come out
	// the same.
	for i, want := range golden.Albums {
		got := scan.Albums[i]
		if got.RootIndex != want.RootIndex || got.Relpath != want.Relpath ||
			got.Name != want.Name || got.Artist != deref(want.Artist) {
			t.Errorf("album[%d] = {root %d, relpath %q, name %q, artist %q}, want %+v",
				i, got.RootIndex, got.Relpath, got.Name, got.Artist, want)
		}
		keys := make([]string, 0, len(got.TrackKeys))
		for key := range got.TrackKeys {
			keys = append(keys, key)
		}
		if got := sorted(keys); !reflect.DeepEqual(got, want.TrackKeys) {
			t.Errorf("album[%d] %q track keys = %q, want %q", i, want.Relpath, got, want.TrackKeys)
		}
		if resolved := got.Resolved(scan.Roots); resolved != want.Resolved {
			t.Errorf("album[%d] resolved = %q, want %q", i, resolved, want.Resolved)
		}
	}

	got := map[string][]string{}
	for key, members := range scan.ByName {
		relpaths := make([]string, 0, len(members))
		for _, album := range members {
			relpaths = append(relpaths, album.Relpath)
		}
		got[key] = relpaths
	}
	if !reflect.DeepEqual(got, golden.ByName) {
		t.Errorf("by_name = %v, want %v", got, golden.ByName)
	}
}

// TestRootScopeIsNotIndexedUnderItsOwnBasename pins the exclusion `scan_roots`
// makes: a root that holds audio directly is an album scope, but its name is "",
// so a mount-point basename never joins the group of a real album with that name.
func TestRootScopeIsNotIndexedUnderItsOwnBasename(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	scan := library.ScanRoots([]string{"testdata/synth/lib1"})
	if _, ok := scan.ByName["lib1"]; ok {
		t.Fatalf("by_name indexed the root's own basename: %v", scan.ByName["lib1"])
	}
	var rootScope *library.AlbumDir
	for _, album := range scan.Albums {
		if album.Relpath == library.RootScope {
			rootScope = album
		}
	}
	if rootScope == nil {
		t.Fatal("no album scope for the root itself, but the fixture puts a loose file there")
	}
	if rootScope.Name != "" || rootScope.Artist != "" {
		t.Errorf("root scope name/artist = %q/%q, want empty", rootScope.Name, rootScope.Artist)
	}
	// The dangling symlink named like audio is a *file* to CPython's os.walk, so
	// it contributes a key; a symlinked directory is a directory and is not
	// descended into.
	if _, ok := rootScope.TrackKeys["dangling"]; !ok {
		t.Errorf("root scope keys = %v, want the dangling symlink counted as a file", rootScope.TrackKeys)
	}
	for _, album := range scan.Albums {
		if album.Relpath == "Linked" || album.Relpath == "Linked/TEMPLIME" {
			t.Errorf("descended into a symlinked directory: %q", album.Relpath)
		}
	}
}

func TestUnreachableRootKeepsItsSlot(t *testing.T) {
	testutil.ChdirModuleRoot(t)
	scan := library.ScanRoots([]string{
		"testdata/synth/lib1",
		"testdata/synth/definitely-not-here",
		"testdata/synth/lib2",
	})
	if len(scan.Roots) != 3 || len(scan.Reachable) != 3 {
		t.Fatalf("roots/reachable = %v/%v, want three slots", scan.Roots, scan.Reachable)
	}
	if scan.Reachable[1] {
		t.Error("a missing root reported as reachable")
	}
	// The missing drive must not take down the other two: this is the whole
	// reason `scan_roots` does not raise.
	counts := scan.PerRoot()
	if counts[0] == 0 || counts[2] == 0 {
		t.Fatalf("per_root = %v, want the surviving roots to have albums", counts)
	}
	for _, album := range scan.Albums {
		if album.RootIndex == 1 {
			t.Fatalf("an unreachable root produced an album: %+v", album)
		}
	}
}

func TestCleanPathMatchesPathlib(t *testing.T) {
	cases := map[string]string{
		"":           ".",
		".":          ".",
		"/":          "/",
		"a/../b":     "a/../b", // pathlib does not resolve ".."
		"a/./b":      "a/b",
		"//a/b":      "//a/b",
		"///a/b":     "/a/b",
		"a//b/":      "a/b",
		"/library/":  "/library",
		"/x/./y//z/": "/x/y/z",
		"../up":      "../up",
		"/a/../../b": "/a/../../b",
	}
	for input, want := range cases {
		if got := library.CleanPath(input); got != want {
			t.Errorf("CleanPath(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestJoinPathMatchesPathlib(t *testing.T) {
	cases := []struct{ base, rel, want string }{
		{"/library", ".", "/library"},
		{"/library", "x", "/library/x"},
		{"/", "x", "/x"},
		{".", "x", "x"},
		{"library", "artist/album", "library/artist/album"},
		{"//a", "b", "//a/b"},
	}
	for _, c := range cases {
		if got := library.JoinPath(c.base, c.rel); got != c.want {
			t.Errorf("JoinPath(%q, %q) = %q, want %q", c.base, c.rel, got, c.want)
		}
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func sorted(in []string) []string {
	out := append([]string(nil), in...)
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j] < out[j-1]; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}
