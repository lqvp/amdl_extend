// Package testutil locates the module root and reads the golden corpus.
//
// The corpus in `testdata/golden/` is *output of the Python implementation*
// (`tools/gen_golden.py`), not a hand-written expectation, and both sides of the
// comparison run with the module root as their working directory -- which is why
// every golden test chdirs there and back. Roots recorded as
// `testdata/synth/lib1` have to mean the same directory to a test binary whose
// own working directory is `internal/library`, and the alternative (rewriting the
// paths on the Go side) would test a walk of a root the Python side never
// walked.
package testutil

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// ModuleRoot is the directory holding go.mod, derived from this file's own
// location rather than from the working directory -- the same rule the port uses
// everywhere else, and for the same reason.
func ModuleRoot() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		panic("testutil: runtime.Caller failed")
	}
	return filepath.Dir(filepath.Dir(filepath.Dir(file)))
}

// ChdirModuleRoot makes the test run where the Python generator ran, and puts the
// working directory back afterwards.
//
// Restoring matters as much as setting it: `t.Parallel` and any later test in the
// same binary share the process, and a test that leaves the process somewhere
// else makes the *next* failure incomprehensible.
func ChdirModuleRoot(t *testing.T) {
	t.Helper()
	previous, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	if err := os.Chdir(ModuleRoot()); err != nil {
		t.Fatalf("chdir to module root: %v", err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(previous); err != nil {
			t.Fatalf("restore working directory: %v", err)
		}
	})
}

// LoadGolden decodes `testdata/golden/<name>` into `out`.
func LoadGolden(t *testing.T, name string, out any) {
	t.Helper()
	path := filepath.Join(ModuleRoot(), "testdata", "golden", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v (run tools/gen_golden.py)", path, err)
	}
	if err := json.Unmarshal(raw, out); err != nil {
		t.Fatalf("decode %s: %v", path, err)
	}
}
