// Package bench measures the two operations the port exists for, over the real
// tree rather than a fixture.
//
// It is a *test* package with benchmarks in it rather than a `main`, so that the
// measurement shares the build, the compiler flags and the code that ships --
// `go test -bench . -run XXX ./bench` -- and so that nothing here can drift into
// being a second implementation of the thing under test.
//
// The tree comes from `AMDHUB_BENCH_LIBRARY`, which
// `tools/gen_bench_library.py` builds; an unset variable skips rather than fails,
// because a benchmark nobody has generated a library for is not a broken build.
// `tools/bench_compare.sh` runs this and the Python side over one tree and prints
// both.
package bench

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"amdhub/internal/dedup"
	"amdhub/internal/library"
	"amdhub/internal/normalize"
)

func benchRoot(tb testing.TB) string {
	tb.Helper()
	root := os.Getenv("AMDHUB_BENCH_LIBRARY")
	if root == "" {
		tb.Skip("set AMDHUB_BENCH_LIBRARY (tools/gen_bench_library.py builds one)")
	}
	return root
}

// BenchmarkScanRoots is the whole `scan_roots`, which is what a request costs:
// the design re-walks the filesystem every time, deliberately, so this is not a
// cold-start measurement but the steady-state one.
func BenchmarkScanRoots(b *testing.B) {
	root := benchRoot(b)
	// One warm-up scan, so the page cache and the directory entries are not part
	// of the first iteration's number.
	library.ScanRoots([]string{root})
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		library.ScanRoots([]string{root})
	}
}

// BenchmarkFindDuplicate is the per-track decision, over an album scope that
// really exists in the tree: the scan is taken once, outside the timer, because
// what is being measured is the lookup and not the walk it needs.
func BenchmarkFindDuplicate(b *testing.B) {
	root := benchRoot(b)
	scan := library.ScanRoots([]string{root})
	if len(scan.Albums) == 0 {
		b.Fatalf("no albums under %s", root)
	}

	type query struct {
		album  string
		title  string
		artist string
	}
	queries := make([]query, 0, len(scan.Albums))
	for _, album := range scan.Albums {
		// The empty key is a real value in this library -- 6 tracks in the real
		// one fold to "" -- and `FindDuplicate` refuses to match on it, correctly.
		// A benchmark that asked with it would be measuring the refusal.
		for key := range album.TrackKeys {
			if key != "" {
				queries = append(queries, query{album.Name, key, album.Artist})
				break
			}
		}
	}
	sort.Slice(queries, func(i, j int) bool { return queries[i].album < queries[j].album })

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		q := queries[i%len(queries)]
		hit, err := dedup.FindDuplicate(scan, q.album, q.title, q.artist, dedup.ScopeLoose)
		if err != nil {
			b.Fatalf("findDuplicate: %v", err)
		}
		if hit == nil {
			b.Fatalf("no hit for %q/%q, which came out of the scan", q.album, q.title)
		}
	}
}

// BenchmarkNormalize is the fold on its own, because it is the one part of the
// scan that is CPU rather than I/O and the one the port had to reimplement
// (CPython's `unicodedata` against this port's generated tables).
func BenchmarkNormalize(b *testing.B) {
	root := benchRoot(b)
	var names []string
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		names = append(names, info.Name())
		if len(names) >= 50000 {
			return filepath.SkipDir
		}
		return nil
	})
	if err != nil {
		b.Fatalf("walk: %v", err)
	}
	if len(names) == 0 {
		b.Fatalf("no names under %s", root)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		normalize.Normalize(names[i%len(names)], true)
	}
}
