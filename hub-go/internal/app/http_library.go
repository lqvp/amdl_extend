package app

import (
	"net/http"

	"amdhub/internal/library"
)

// handleLibraryAlbums is every album scope across every root, plus which roots could
// not be read.
//
// `per_root` is positional with `roots` and is the answer to the question a total
// cannot answer: *is each root actually contributing?* An external drive that is not
// plugged in can be mounted-and-empty rather than missing, and that reads as a healthy
// root with nothing in it -- no degraded row, no warning, and a silent re-download of
// everything that lived on it. The counts are what make that visible, so they are in
// the response and on the page.
func (s *State) handleLibraryAlbums(w http.ResponseWriter, r *http.Request) {
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	writeJSON(w, http.StatusOK, listingFor(scan))
}

// handleLibraryArtists is the same walk, artists only. A separate walk rather than a
// cache, because the library on disk is the only source of truth.
func (s *State) handleLibraryArtists(w http.ResponseWriter, r *http.Request) {
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	writeJSON(w, http.StatusOK, map[string]any{"artists": listingFor(scan).Artists})
}

// handleLibraryScan is a scan, and its result.
//
// The endpoint is the API's "invalidate the walk cache" -- and there is nothing to
// invalidate, because no walk is ever held. So it is a scan on demand, which is the
// useful half of the same idea: an operator who has just plugged a drive in can check
// that the hub sees it without waiting for the next page load.
func (s *State) handleLibraryScan(w http.ResponseWriter, r *http.Request) {
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	view := listingFor(scan)
	writeJSON(w, http.StatusOK, map[string]any{
		"roots":          view.Roots,
		"degraded_roots": view.DegradedRoots,
		"per_root":       view.PerRoot,
		"albums":         len(view.Albums),
	})
}

// handleLibraryDuplicates is not in this build, and says so.
//
// The read-only duplicate report is a later phase. It is *not* the same thing as a skip
// reason: that carries the paths a `loose` skip matched, for one track, so a human can
// overrule it -- and the design refuses the cross-album group view outright, because a
// title shared by 1,207 of 8,721 library keys is not evidence of a duplicate. A caller
// reaching this gets an explanation rather than a 404.
func (s *State) handleLibraryDuplicates(w http.ResponseWriter, r *http.Request) {
	fail(w, http.StatusNotImplemented, "the read-only duplicate report is Phase 2. What "+
		"this build does show is the other half: a skipped job's `skip_reason` names every "+
		"path its `loose` match hit, in GET /api/jobs and in the queue page, which is what "+
		"makes one skip adjudicable by hand.")
}
