// Package library is a port of `hub/library_scan.py`: album discovery across
// every configured library root.
//
// The filesystem is the only source of truth and nothing here is cached. The
// Python original measures a full `scan_roots` of the same 341 GB / 3,670-album
// library at 0.091 s (median of 5: walk 0.044, extension filter 0.005, normalize
// 0.015, building 3,670 album scopes 0.038, by_name 0.009), so every request
// re-walks, a directory the user moved outside the app is followed immediately,
// and there is no scan cache to invalidate and no staleness window to reason
// about.
//
// The port keeps that, and adds the one thing the original could not have: the
// walk is per-root and the roots are independent, so `ScanRoots` can walk them
// concurrently. It is still a fresh walk per request.
//
// What a walk yields is deliberately *not* a model of the library.
// `dirPathFormat` describes one of the two libraries on this machine and not the
// other, and neither matches the shapes the downloader actually produces over
// time (loose files in an artist directory, a subdirectory sitting next to a
// loose file, an album directory holding a `.part` leftover). So the scan reports
// the one fact that is unambiguous -- **a directory that directly holds at least
// one audio file is an album scope** -- and derives artist from the structure as
// a best effort that is allowed to be absent.
package library

import (
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"

	"amdhub/internal/normalize"
)

// RootScope is the `relpath` of the album scope that a root is, when the root
// holds audio directly.
//
// `Path.relative_to` answers "." for a path against itself, so `Roots[i]` joined
// with this keeps naming the directory, and it is the one value that identifies a
// scope by position rather than by name -- which is why `ScanRoots` keeps it out
// of `ByName` and gives it an empty `Name`.
const RootScope = "."

// AlbumDir is one album scope: a directory that directly holds at least one audio
// file.
type AlbumDir struct {
	// RootIndex is the position of the root in `Scan.Roots`, so an album dir can
	// always be resolved back to an absolute path for display or for serving a
	// file.
	RootIndex int
	// Relpath is posix-form and relative to **the root exactly as the caller
	// passed it**; `ScanRoots` does not resolve anything.
	Relpath string
	Name    string
	// Artist is the parent directory's name, or "" where there is none below the
	// root to read one from. See `artist`.
	Artist string
	// TrackKeys is `frozenset(normalize(name) for name in audio)`: basenames
	// only, so a key is never a path, and an empty key is *kept* -- `normalize`
	// reports an unusable title rather than hiding it, so the caller can find it
	// and refuse to skip on it.
	TrackKeys map[string]struct{}
}

// Resolved is `Roots[RootIndex] / Relpath` -- the form to show a human, and the
// form `skip_reason` carries. For a root that is itself an album scope `Relpath`
// is "." and the join collapses it, so the entry is the root rather than a
// trailing "/.".
func (a *AlbumDir) Resolved(roots []string) string {
	return JoinPath(roots[a.RootIndex], a.Relpath)
}

// Scan is the result of one walk of every root, plus what to warn the user about.
type Scan struct {
	// Roots and Reachable are positional and always the same length, including
	// for a root that could not be read. A rejected root keeps its slot so that
	// RootIndex means the same thing to every AlbumDir in the scan, and so the UI
	// can name the drive that is missing instead of silently deduping against a
	// smaller library.
	Roots     []string
	Reachable []bool
	Albums    []*AlbumDir
	ByName    map[string][]*AlbumDir
}

// Degraded is the roots that could not be read, in the order the caller passed
// them.
//
// An unmounted external drive must be *loud*. Loose dedup against the surviving
// roots still works, so the failure mode without this is a quiet re-download of
// everything that lived on the missing drive.
func (s *Scan) Degraded() []string {
	var out []string
	for i, ok := range s.Reachable {
		if !ok {
			out = append(out, s.Roots[i])
		}
	}
	return out
}

// PerRoot is the album-directory count found under each root, positional like
// Roots and Reachable.
//
// **This exists because `Degraded` cannot catch the failure it looks like it
// catches.** A root that is *unreadable* is reported, loudly. A root that is
// **mounted and empty** is not: Docker's bind-mount autocreate makes a directory
// when the source is missing, so an absent drive becomes a present, readable,
// zero-album root that `Reachable` reports as true. Nothing on disk distinguishes
// an empty library from an absent drive, so the count has to be surfaced rather
// than inferred -- and a *per-root* count is the only shape that can be, because
// a total is equally consistent with both roots working and with one of them
// empty.
func (s *Scan) PerRoot() []int {
	counts := make([]int, len(s.Roots))
	for _, album := range s.Albums {
		counts[album.RootIndex]++
	}
	return counts
}

// ScanRoots walks every root and returns the album scopes found across all of
// them.
//
// A root is *reachable* when it is a directory. An unreachable one keeps its slot
// in Roots/Reachable, is listed by `Degraded`, and contributes no albums; the
// other roots are unaffected. Nothing here raises for a bad root, because a
// single missing 341 GB drive must not take down dedup for the 69 GB one next to
// it.
//
// Roots are walked concurrently. They are independent by construction -- nothing
// in a walk reads another root -- and the merge below keeps the result
// *positional* and byte-identical to what a sequential walk would produce, which
// is what `TestScanMatchesPython` pins. On the real library the walk is 0.044 s
// of the 0.091 s, so this is the one part of the scan that scales with hardware
// rather than with concurrency tricks.
func ScanRoots(roots []string) *Scan {
	cleaned := make([]string, len(roots))
	for i, root := range roots {
		cleaned[i] = CleanPath(root)
	}
	scan := &Scan{
		Roots:     cleaned,
		Reachable: make([]bool, len(cleaned)),
		ByName:    map[string][]*AlbumDir{},
	}

	perRoot := make([][]*AlbumDir, len(cleaned))
	var wg sync.WaitGroup
	for i, root := range cleaned {
		wg.Add(1)
		go func(i int, root string) {
			defer wg.Done()
			info, err := os.Stat(root)
			if err != nil || !info.IsDir() {
				return
			}
			scan.Reachable[i] = true
			perRoot[i] = walkRoot(i, root)
		}(i, root)
	}
	wg.Wait()

	for _, albums := range perRoot {
		for _, album := range albums {
			scan.Albums = append(scan.Albums, album)
			if album.Relpath == RootScope {
				// The root is a container that happens to hold audio, not an
				// album, and `ByName` is keyed by album *name*. Indexing it would
				// put a mount-point basename -- "Music", "my-music", a volume
				// UUID -- into that index, where it would silently join the group
				// of a real album with that name and be matched against as one.
				// It is worse than a latent collision: the same drive reached by
				// two different spellings would produce two different keys.
				//
				// Nothing is lost by leaving the scope out of the index. It keeps
				// its place in `Albums`, and the only route to it before this
				// exclusion ran was a lookup by the mount point's own basename --
				// "Music", "my-music" -- which is never the album name of a
				// download, so no re-request could have matched it.
				continue
			}
			key := normalize.AlbumKey(album.Name)
			scan.ByName[key] = append(scan.ByName[key], album)
		}
	}
	return scan
}

// walkRoot yields the album scopes under one root, in a stable order.
//
// **`root` is used as given and is never resolved.** The user's own path is
// typically a user-managed symlink into `/run/media/<volume-UUID>/`, so `relpath`
// has to be cut against the string that was passed in. Resolving the root but not
// the paths the walk yields (or the reverse) makes every entry look like it lives
// outside the root, and the whole scan then quietly finds nothing -- no
// exception, no album dir, every download running again. `relpathOf` raises
// rather than answering in that case, for the same reason.
//
// The walk descends through album directories rather than stopping at them, and
// that is not a contradiction of "an album dir is a scope": the real library has
// `TEMPLIME/HIKO.flac` and `TEMPLIME/Escapism/` side by side, so `TEMPLIME` and
// `TEMPLIME/Escapism` are two separate scopes. Not descending would apply to
// building the *key set*, which never merges a child's keys into its parent, and
// not to the traversal.
func walkRoot(rootIndex int, root string) []*AlbumDir {
	prefix := root
	if !strings.HasSuffix(prefix, string(os.PathSeparator)) {
		prefix += "/"
	}
	walker := &walker{prefix: prefix, sem: make(chan struct{}, walkParallelism())}
	return walker.walk(rootIndex, root)
}

// walkParallelism bounds how many directories are walked at once.
//
// The pool is a channel rather than an unbounded `go` per directory because a
// 4,000-album library is ~5,000 directories, and 5,000 goroutines each doing one
// `ReadDir` would spend more time in the scheduler than in the syscall. Four per
// available CPU is the usual shape for an I/O-bound walk: enough overlap to keep
// the disk and the cores busy, not so much that the in-flight set is unbounded.
func walkParallelism() int {
	limit := 4 * runtime.GOMAXPROCS(0)
	if limit < 2 {
		limit = 2
	}
	return limit
}

// walker holds the walk's one piece of shared state -- the concurrency limit --
// and the root prefix the relpaths are cut against.
type walker struct {
	prefix string
	sem    chan struct{}
}

// walk yields the album scopes under one directory, in a stable order, and
// **descends into subdirectories concurrently**.
//
// Order is what makes this more than a `go` per directory: a directory's own
// scope comes first, then its children in name order, which is exactly what
// CPython's `os.walk` yields and what `TestScanMatchesPython` pins against a
// generated corpus. Concurrency is bounded by `sem` and the results are
// reassembled positionally, so the answer is byte-identical to the sequential
// one -- the only thing parallelism changes is how long it takes.
//
// The recursion is what makes it worth doing at all on the deployment's common
// shape: `AMD_LIBRARY_ROOTS` is usually *one* root, so per-root concurrency would
// give a single-threaded walk, and the walk is where 0.044 s of the Python
// original's 0.091 s scan is spent.
//
// Nothing below the root is resolved or cleaned -- see the package comment for
// why that is load-bearing rather than fastidious.
func (w *walker) walk(rootIndex int, dir string) []*AlbumDir {
	entries, err := os.ReadDir(dir)
	if err != nil {
		// `os.walk` with a default `onerror` skips a directory it cannot read, and
		// `os.ReadDir` does the same for the directory itself. A permission-denied
		// subtree must not abort the other 3,000 albums.
		return nil
	}
	// readdir order is filesystem-dependent -- NTFS does not sort -- and relpaths
	// are shown to the user in `skip_reason`, so two scans of one unchanged tree
	// have to come out in the same order. `os.ReadDir` sorts by name, which is the
	// same order `dirnames.sort()` produces.
	var dirs []string
	var audio []string
	for _, entry := range entries {
		if isDirEntry(dir, entry) {
			dirs = append(dirs, entry.Name())
			continue
		}
		if normalize.IsAudioFile(entry.Name()) {
			audio = append(audio, entry.Name())
		}
	}

	var out []*AlbumDir
	if len(audio) > 0 {
		out = append(out, w.scope(rootIndex, dir, audio))
	}
	if len(dirs) == 0 {
		return out
	}

	// One child is walked inline: spawning a goroutine for a chain of
	// single-child directories would be all overhead and no parallelism.
	if len(dirs) == 1 {
		return append(out, w.walk(rootIndex, dir+"/"+dirs[0])...)
	}

	children := make([][]*AlbumDir, len(dirs))
	var wg sync.WaitGroup
	for i, child := range dirs {
		path := dir + "/" + child
		select {
		case w.sem <- struct{}{}:
			wg.Add(1)
			go func(i int, path string) {
				defer wg.Done()
				defer func() { <-w.sem }()
				children[i] = w.walk(rootIndex, path)
			}(i, path)
		default:
			// The pool is full: this goroutine does the child itself, which is
			// what keeps the work moving without more goroutines.
			children[i] = w.walk(rootIndex, path)
		}
	}
	wg.Wait()
	for _, child := range children {
		out = append(out, child...)
	}
	return out
}

// scope is the one unambiguous fact a walk yields: a directory that directly
// holds at least one audio file is an album scope.
func (w *walker) scope(rootIndex int, dir string, audio []string) *AlbumDir {
	relpath := relpathOf(dir, strings.TrimSuffix(w.prefix, "/"), w.prefix)
	// `.part` is already gone because `IsAudioFile` rejects it: 160 of those are
	// the real library's leftovers from interrupted downloads, and counting one as
	// an existing track would wrongly skip a re-request.
	keys := make(map[string]struct{}, len(audio))
	for _, name := range audio {
		keys[normalize.Normalize(name, true)] = struct{}{}
	}
	parts := []string{}
	if relpath != RootScope {
		parts = strings.Split(relpath, "/")
	}
	// A root that holds audio directly is still an album scope -- the real library
	// has a loose track sitting in it, and leaving those files out would make them
	// invisible to dedup, silently.
	//
	// Its name is "" rather than the root's own basename. No directory on any
	// filesystem can have an empty name, so "" cannot be the name of a real album;
	// a basename taken from the caller's spelling of the path can (the same drive
	// is "Music" at one path and "my-music" at another), which would make
	// `Scan.Albums` depend on how the root was configured.
	name := ""
	if len(parts) > 0 {
		name = parts[len(parts)-1]
	}
	return &AlbumDir{
		RootIndex: rootIndex,
		Relpath:   relpath,
		Name:      name,
		Artist:    artist(parts),
		TrackKeys: keys,
	}
}

// isDirEntry mirrors CPython's `os.walk`, which asks `entry.is_dir()` -- and
// that call *follows symlinks*. A symlinked directory is therefore a directory
// here (and, because `followlinks` is false, is listed but never descended into),
// while a dangling symlink is a file. Go's `DirEntry.IsDir` answers from the
// directory entry's own type, so a symlink needs the extra stat to agree.
func isDirEntry(dir string, entry fs.DirEntry) bool {
	if entry.IsDir() {
		return true
	}
	if entry.Type()&fs.ModeSymlink == 0 {
		return false
	}
	info, err := os.Stat(filepath.Join(dir, entry.Name()))
	return err == nil && info.IsDir()
}

// relpathOf is `dirpath` relative to the root string the caller passed, in posix
// form.
//
// A lexical slice rather than `filepath.Rel`, deliberately: `filepath.Rel`
// cleans both sides, and cleaning a root is exactly the mistake this module must
// not make. RootScope is what the walk yields for the root itself, and it is also
// `Path.relative_to` self's own answer, so joining the root with it still names
// the directory.
func relpathOf(dirpath, root, prefix string) string {
	if dirpath == root {
		return RootScope
	}
	if !strings.HasPrefix(dirpath, prefix) {
		// Unreachable: the walk only ever joins names onto the root it was
		// handed. If this fires, someone resolved one side of the walk and not
		// the other, and the alternative to panicking here is a scan that
		// reports an empty library.
		panic("library: " + dirpath + " is not below the scanned root " + root +
			"; the root and the walked paths must both be used unresolved")
	}
	return dirpath[len(prefix):]
}

// artist is the parent directory's name, or "" where there is none.
//
// `parts` is the relative path split into components, album directory included,
// so `parts[len-1]` is the album and `parts[len-2]` is its parent. That is the
// whole rule, and it is the shape `dirPathFormat` produces: 3,660 of the 3,670
// album directories in the real external library and all 1,069 in `downloads/`
// are `artist/album/`, and `ALAC/Atmos/TEMPLIME/POP-AID` still answers TEMPLIME
// because a format bucket sits between the artist and the root, never between the
// artist and the album.
//
// "" means there is no parent below the root to read a name from: the album
// directory *is* the root, or it sits directly in it. `Music/Nyarons/A.flac` is
// the second case, and there are 9 more like it in the real library. Reporting
// the root's own name instead would put "Music" in the library UI and make
// `strict` matching compare against it, and `strict` already treats a missing
// artist as "cannot vouch for this".
//
// `ALAC/Atmos/Some Album/` -- an album with no artist level, so the parent is a
// codec directory -- is answered as "Atmos". That is a known wrong answer,
// accepted because the real library contains no such directory and "" there would
// be no less of a guess. It is the reason the rule has no codec-bucket exception:
// an exception that only fires on inputs that do not exist costs a reader more
// than it saves.
func artist(parts []string) string {
	if len(parts) < 2 {
		return ""
	}
	return parts[len(parts)-2]
}

// CleanPath is `pathlib.PurePosixPath(p)`: it collapses repeated separators,
// drops "." components, removes a trailing separator, and does **not** touch
// "..", resolve a symlink, or make a relative path absolute.
//
// Mirrored rather than replaced with `filepath.Clean` because the two disagree in
// three ways that are observable here: `filepath.Clean` resolves "..", drops the
// POSIX double-slash root, and turns "" into ".", which `PurePosixPath` also does
// -- but `Clean("a/../../b")` is "../b" to Clean and "a/../../b" to pathlib.
//
// `ScanRoots` cleans each root once, on the way in, and nothing else in this
// package cleans anything: the walk's `relpath` arithmetic is a lexical slice of
// exactly the string the caller passed.
func CleanPath(p string) string {
	root := ""
	rest := p
	if strings.HasPrefix(rest, "/") {
		if strings.HasPrefix(rest, "//") && !strings.HasPrefix(rest, "///") {
			root, rest = "//", strings.TrimLeft(rest, "/") // POSIX: exactly two is implementation-defined
		} else {
			root, rest = "/", strings.TrimLeft(rest, "/")
		}
	}
	var parts []string
	for _, part := range strings.Split(rest, "/") {
		if part == "" || part == "." {
			continue
		}
		parts = append(parts, part)
	}
	if len(parts) == 0 {
		switch root {
		case "":
			return "."
		case "//":
			return "//"
		default:
			return "/"
		}
	}
	joined := strings.Join(parts, "/")
	if root == "" {
		return joined
	}
	if root == "//" {
		return "//" + joined
	}
	return "/" + joined
}

// JoinPath is `PurePosixPath(base) / rel` rendered back to a string, which is
// what `str(roots[i] / relpath)` in `dedup.find_duplicate` does.
//
// The absolute/relative rule is pathlib's: joining onto an empty or "." base
// yields the relative path, and a "." relpath yields the base unchanged (which is
// why a root that is itself an album scope reports the root rather than
// "/<root>/.").
func JoinPath(base, rel string) string {
	if rel == RootScope || rel == "" {
		return CleanPath(base)
	}
	base = CleanPath(base)
	if strings.HasPrefix(rel, "/") {
		return CleanPath(rel)
	}
	if base == "." || base == "" {
		return CleanPath(rel)
	}
	if base == "/" {
		return CleanPath("/" + rel)
	}
	if base == "//" {
		return "//" + rel
	}
	return base + "/" + rel
}

// SortedRelpaths returns the relpaths in the order the Python implementation
// would sort them, which is what `DuplicateHit.Matched` is built from.
func SortedRelpaths(albums []*AlbumDir) []string {
	out := make([]string, 0, len(albums))
	for _, album := range albums {
		out = append(out, album.Relpath)
	}
	sort.Strings(out)
	return out
}
