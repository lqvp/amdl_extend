package app

import (
	"bytes"
	"embed"
	"fmt"
	"html/template"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"

	"amdhub/internal/jobs"
	"amdhub/internal/library"
)

// The templates and the two assets, embedded.
//
// **Embedded rather than read from disk**, which is a deployment decision: the binary
// is the whole hub, so there is no "the templates are in the image but not next to the
// binary" failure, and no working directory for a page to depend on. `app.js` in
// particular is the client contract -- it reads the JSON shapes the API produces -- so
// the two travel together.
//
//go:embed templates/*.html static/*
var assets embed.FS

// staticAssets is the `/static` mount's content, by file name.
var staticAssets = map[string]string{}

func init() {
	for _, name := range []string{"app.css", "app.js"} {
		content, err := assets.ReadFile("static/" + name)
		if err != nil {
			panic(err)
		}
		staticAssets[name] = string(content)
	}
}

// templates is one parsed set per page, built once.
//
// **One set per page, and that is what makes the layout work.** Every page file defines
// a template called `content` and `base.html` renders it, so parsing all of them into
// one set would leave whichever page was parsed last answering for all four -- a queue
// page rendering the library, silently. `html/template` caches nothing across parses,
// so building the four sets once at start-up is also what keeps a page load from
// re-reading the files.
var templates = func() map[string]*template.Template {
	base, err := assets.ReadFile("templates/base.html")
	if err != nil {
		panic(err)
	}
	sets := map[string]*template.Template{}
	for _, name := range []string{"login", "queue", "library"} {
		page, err := assets.ReadFile("templates/" + name + ".html")
		if err != nil {
			panic(err)
		}
		set := template.New("base.html").Funcs(template.FuncMap{
			"join":    strings.Join,
			"percent": percent,
		})
		set = template.Must(set.Parse(string(base)))
		set = template.Must(set.Parse(string(page)))
		// The row comes from its own file, so the queue table and the stream's
		// client-side `buildRow` are two renderings of one contract.
		if name == "queue" {
			row, err := assets.ReadFile("templates/job_row.html")
			if err != nil {
				panic(err)
			}
			set = template.Must(set.Parse(string(row)))
		}
		sets[name] = set
	}
	return sets
}()

func percent(fraction *float64) string {
	if fraction == nil {
		return ""
	}
	return strconv.FormatFloat(*fraction*100, 'f', 1, 64)
}

// baseView is what every page is given.
type baseView struct {
	Title         string
	BodyAttrs     template.HTMLAttr
	Authenticated bool
}

type loginView struct {
	baseView
	Failed    bool
	Message   string
	WrapperOK bool
	Binary    string
}

// jobRowView is one row of the queue table, which is a *rendering* of a job rather
// than the wire shape.
//
// The two are deliberately separate and deliberately consistent: `app.js`'s `buildRow`
// rebuilds every row from the JSON on the first stream frame, so a row this file
// renders is replaced by one the browser builds. `newJobRow` is the only place the
// split is expressed, so the two cannot drift by editing one and not the other.
type jobRowView struct {
	ID        int64
	Title     string
	Status    string
	Codec     string
	Progress  *float64
	Error     *string
	ParentURL string
	SkipPaths []string
	Finished  bool
}

func newJobRow(job *jobs.Job) jobRowView {
	row := jobRowView{
		ID:        job.ID,
		Status:    job.Status,
		Codec:     job.Codec,
		Progress:  job.Progress,
		Error:     job.Error,
		ParentURL: job.ParentURL,
		Finished:  isFinished(job.Status),
	}
	if job.Title != nil {
		row.Title = *job.Title
	}
	if job.SkipReason != nil {
		raw := strings.TrimPrefix(*job.SkipReason, "duplicate:")
		for _, path := range strings.Split(raw, "|") {
			if path != "" {
				row.SkipPaths = append(row.SkipPaths, path)
			}
		}
	}
	return row
}

// isFinished is the set the template and `app.js` both encode, in one place so they
// cannot disagree about which statuses collapse.
func isFinished(status string) bool {
	switch status {
	case "done", "failed", "skipped", "cancelled":
		return true
	}
	return false
}

type countView struct {
	Status string
	Count  int
}

// rootView is one row of the library's per-root table, which is the shape that
// answers the question a total cannot: *is each root actually contributing?*
type rootView struct {
	Path     string
	Count    int
	Degraded bool
}

// albumView is one album scope, with its absolute path.
//
// The absolute path is `roots[root_index] / relpath` and nothing else: the scan does
// not resolve anything, because the user's own path is a symlink and resolving one
// side of a comparison is how a scan ends up finding nothing. The same expression is
// what a skip reason carries, so the two name a directory by the same route.
type albumView struct {
	Name    string `json:"name"`
	Artist  string `json:"artist"`
	Root    string `json:"root"`
	Relpath string `json:"relpath"`
	Path    string `json:"path"`
	Tracks  int    `json:"tracks"`
}

// rootRowView is one row of the library page's per-root table.
//
// Derived, because the API's shape is three *parallel* lists (`roots`, `per_root`, and
// the subset in `degraded_roots`) and a template that walks them by index is where a
// page silently shows one root's count against another root's path. The JSON keeps the
// parallel shape -- it is the API's contract -- and this is what the table iterates.
type rootRowView struct {
	Path     string
	Count    int
	Degraded bool
}

// listingView is the library page and `/api/library/albums`, from one walk.
type listingView struct {
	Roots         []string      `json:"roots"`
	DegradedRoots []string      `json:"degraded_roots"`
	PerRoot       []int         `json:"per_root"`
	Albums        []albumView   `json:"albums"`
	Artists       []string      `json:"artists"`
	RootRows      []rootRowView `json:"-"`
}

// listingFor turns one walk into the page's and the API's shape.
//
// A real walk, on every request, and that is the rule: the library on disk is the only
// source of truth, so a drive that was unplugged a second ago has to show up here
// rather than in a cache's opinion of a minute ago.
func listingFor(scan *library.Scan) listingView {
	view := listingView{
		Roots:         append([]string(nil), scan.Roots...),
		DegradedRoots: scan.Degraded(),
		PerRoot:       scan.PerRoot(),
	}
	if view.Roots == nil {
		view.Roots = []string{}
	}
	if view.DegradedRoots == nil {
		view.DegradedRoots = []string{}
	}
	degraded := map[string]bool{}
	for _, root := range view.DegradedRoots {
		degraded[root] = true
	}
	for index, root := range view.Roots {
		count := 0
		if index < len(view.PerRoot) {
			count = view.PerRoot[index]
		}
		view.RootRows = append(view.RootRows, rootRowView{
			Path: root, Count: count, Degraded: degraded[root],
		})
	}
	if view.RootRows == nil {
		view.RootRows = []rootRowView{}
	}

	artists := map[string]bool{}
	for _, album := range scan.Albums {
		view.Albums = append(view.Albums, albumView{
			Name:    album.Name,
			Artist:  album.Artist,
			Root:    scan.Roots[album.RootIndex],
			Relpath: album.Relpath,
			Path:    album.Resolved(scan.Roots),
			Tracks:  len(album.TrackKeys),
		})
		if album.Artist != "" {
			artists[album.Artist] = true
		}
	}
	for artist := range artists {
		view.Artists = append(view.Artists, artist)
	}
	sort.Strings(view.Artists)
	if view.Albums == nil {
		view.Albums = []albumView{}
	}
	if view.Artists == nil {
		view.Artists = []string{}
	}
	return view
}

// wrapperView is what the wrapper is doing, as the UI needs it.
//
// Every field is a fact about the wrapper rather than about this hub's opinion of it:
// `Running` is the supervisor's own, `Regions` is the payload's, and `Detail` is
// either a message this package wrote (for the state it observed) or the supervisor's,
// unchanged.
type wrapperView struct {
	Running bool     `json:"running"`
	Adopted bool     `json:"adopted"`
	Pid     int      `json:"pid"`
	Port    int      `json:"port"`
	Regions []string `json:"regions"`
	Ready   bool     `json:"ready"`
	Problem string   `json:"problem"`
	Detail  string   `json:"detail"`
}

type queueView struct {
	baseView
	Wrapper wrapperView
	Library LibrarySummary
	Roots   []rootView
	Jobs    []jobRowView
	Counts  []countView
	Codecs  []string
	Log     []string
}

type libraryPageView struct {
	baseView
	Listing listingView
}

// render writes one page.
//
// **Built into a buffer first**, so a template that fails halfway does not leave half a
// page followed by a JSON error: the status and the content type can only be sent once,
// and "the page could not be rendered" is only useful if it arrives instead of the
// fragment that caused it.
func (s *State) render(w http.ResponseWriter, name string, view any) {
	set, ok := templates[name]
	if !ok {
		fail(w, http.StatusInternalServerError,
			fmt.Sprintf("there is no %s template.", name))
		return
	}
	var buffer bytes.Buffer
	if err := set.ExecuteTemplate(&buffer, "base.html", view); err != nil {
		fail(w, http.StatusInternalServerError, fmt.Sprintf("the %s page could not be "+
			"rendered: %v", name, err))
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(buffer.Bytes())
}

func (s *State) baseView(r *http.Request, title string) baseView {
	return baseView{Title: title, Authenticated: s.authenticated(r)}
}

// handleQueuePage is the queue, with the wrapper's state and the library's
// reachability above it.
//
// One page rather than two, because the two things a user needs at the moment they
// press "download" are the same two things: is anything running, and is the drive I am
// about to write to mounted.
func (s *State) handleQueuePage(w http.ResponseWriter, r *http.Request) {
	all, err := s.Store.List(jobs.ListFilter{})
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	counts, _ := s.Counts()
	view := queueView{
		baseView: s.baseView(r, "Queue · amd-hub"),
		Wrapper:  s.WrapperState(r.Context()),
		Library:  s.LibrarySummary(),
		Codecs:   sortedKeys(Codecs),
		Log:      s.Log.Lines(),
	}
	for _, job := range all {
		view.Jobs = append(view.Jobs, newJobRow(job))
	}
	for index, root := range view.Library.Roots {
		view.Roots = append(view.Roots, rootView{
			Path:     root,
			Count:    view.Library.PerRoot[index],
			Degraded: containsString(view.Library.DegradedRoots, root),
		})
	}
	for status, count := range counts {
		if status == "total" {
			continue
		}
		view.Counts = append(view.Counts, countView{Status: status, Count: count})
	}
	view.Counts = append(view.Counts, countView{Status: "total", Count: counts["total"]})
	s.render(w, "queue", view)
}

// handleLibraryPage is the library listing, read-only.
func (s *State) handleLibraryPage(w http.ResponseWriter, r *http.Request) {
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	s.render(w, "library", libraryPageView{
		baseView: s.baseView(r, "Library · amd-hub"),
		Listing:  listingFor(scan),
	})
}

// handleEnqueueForm is the `POST /enqueue` the queue form names: the no-JS path to
// queueing a URL, which redirects back to the queue with the problems in the log
// rather than in the address bar.
func (s *State) handleEnqueueForm(w http.ResponseWriter, r *http.Request) {
	form := readForm(r)
	body := jobsBody{
		Urls:     splitURLs(form["urls"]),
		Codec:    form["codec"],
		Language: form["language"],
		Force:    form["force"] == "1",
	}
	for _, problem := range s.createJobs(r.Context(), body) {
		s.logLine(fmt.Sprintf("%s: %s", problem.URL, problem.Detail))
	}
	http.Redirect(w, r, "/queue", http.StatusSeeOther)
}

func splitURLs(raw string) []string {
	var out []string
	for _, line := range strings.Split(raw, "\n") {
		if trimmed := strings.TrimSpace(line); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

// handleStatus is the wrapper, the library's reachability, and the queue's shape.
//
// Three sources, none of them cached, and the reason is a deliberate rule: the library
// on disk is the only source of truth, so a drive that was unplugged a second ago has
// to show up here rather than in a cache's opinion of a minute ago.
func (s *State) handleStatus(w http.ResponseWriter, r *http.Request) {
	counts, err := s.Counts()
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"wrapper":            s.WrapperState(r.Context()),
		"library":            s.LibrarySummary(),
		"queue":              counts,
		"dedup_artist_scope": s.Settings.DedupArtistScope,
	})
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func containsString(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}
