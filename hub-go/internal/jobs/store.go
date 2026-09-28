// Package jobs is a port of `hub/jobs.py`: the download queue -- one table, one
// partial unique index, and one atomic claim.
//
// **What is persisted here is `job` and nothing else.** The library on disk is
// the single source of truth for "already downloaded", deliberately, because
// folders get moved, renamed and deleted outside the app and a persistent index
// would go stale against them. So this package never touches the filesystem, and
// there is no `recording` / `release` / `library_file` table to add later: any
// such table would be that stale index, and `dedup.FindDuplicate` already answers
// the only question one would be asked.
//
// **The dedup key is `(adam_id, codec)`, over active jobs only.**
// `job_active_dedupe` is a *partial* unique index, and the three statuses in its
// predicate are the whole of the queue's concurrency story:
//
//   - `language` and `force` are not in the key. Two requests for the same track
//     in two languages are one download, and `force` means "re-download this even
//     if it is on disk" -- which the scheduler's per-file dedup check decides at
//     execution time, not per queue entry.
//   - `waiting` is in the predicate on purpose: a job parked on an expired Apple
//     token must not be re-run alongside a new one, and a *failed* or *cancelled*
//     job must be re-runnable.
//   - `is_music_video` is not in the key either. The `job` table has no column for
//     it; it selects the Widevine path and nothing else.
//
// **The index is the only authority on whether a key is held.** `CreateBatch`
// therefore attempts the insert and reads the resulting constraint failure; it
// never SELECTs first. A SELECT-then-INSERT would have to be correct about a
// concurrent writer as well, and the index already is, for free and inside the
// database. The one thing this buys is an id: the holder's, which the UI needs in
// order to link "already queued" to something.
//
// That path works because the connection is in **autocommit**. Under an implicit
// transaction an INSERT failure would leave the transaction open and every later
// statement on that connection would fail with "cannot start a transaction within
// a transaction" -- the dedup path would poison the store instead of serving it.
// Autocommit also means a batch is not all-or-nothing, which is the right way
// round here: `CreateBatch` is idempotent (the index makes a retry fold into what
// already landed), and a 19-track album must not lose 19 tracks to one unusable
// `adam_id`.
//
// **`ClaimNext` is a single statement, not a read and a write**, because the
// read-then-write form is wrong the moment there are two workers: both read the
// same queued id and both write to it. The whole statement is one write
// transaction, so a second claim blocks on the write lock (`busy_timeout`) and
// then re-evaluates the subquery against what the first one committed.
//
// **Timestamps are the store's, not the caller's.** `started_at` belongs to
// `ClaimNext` alone and `finished_at` is a function of the status: non-NULL
// exactly when the job is terminal. That is what makes a retry land in a
// consistent row without every caller having to remember to clear it.
//
// **One name, two spellings.** The `job` table's columns are `url` / `url_type`;
// the Go-facing `Job` and `CreateBatch` are `ParentURL` / `ParentType`. The
// mapping is the one function, `jobFromRow`.
package jobs

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"amdhub/internal/pyrepr"
	"amdhub/internal/sqlite"
)

// The three vocabularies, as closed sets rather than a bare string a caller can
// walk past. ActiveStatuses is load-bearing -- it is the index's predicate, and
// the two are pinned to each other by tests that put each status on both sides of
// the boundary.
var (
	ActiveStatuses   = []string{"queued", "waiting", "running"}
	TerminalStatuses = []string{"done", "failed", "skipped", "cancelled"}
	AllStatuses      = concat(ActiveStatuses, TerminalStatuses)
)

// The five values `url_type`'s own comment in JobTableSQL lists. A closed set
// because the API layer switches on it to decide between resolving one track and
// resolving an album, so a typo would store cleanly and be rendered as an
// unrecognised kind forever.
var ParentTypes = []string{"song", "album", "artist", "playlist", "music-video"}

// MarkableFields are the four things `Mark` may write besides the status, in a
// fixed order so the SQL it builds is deterministic. `started_at` and
// `finished_at` are absent on purpose: the first is `ClaimNext`'s, the second is
// derived from the status, and a caller that could set either could break both
// invariants.
var MarkableFields = []string{"progress", "bytes_done", "bytes_total", "skip_reason", "error"}

// JobTableSQL is quoted rather than generated, so the artifact a human reads in
// `sqlite_master` is the DDL as it was written down rather than something a loop
// reassembles. The one deviation from the Python source is that this is the same
// text, unchanged -- the port does not get to have its own schema.
const JobTableSQL = `
CREATE TABLE IF NOT EXISTS job (
  id           INTEGER PRIMARY KEY,
  url          TEXT    NOT NULL,
  url_type     TEXT    NOT NULL,   -- song|album|artist|playlist|music-video
  adam_id      TEXT,
  title        TEXT,               -- log display only; never compared
  codec        TEXT    NOT NULL,
  language     TEXT,
  force        INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL,   -- queued|waiting|running|done|failed|skipped|cancelled
  skip_reason  TEXT,
  parent_id    INTEGER REFERENCES job(id),
  progress     REAL,
  bytes_done   INTEGER,
  bytes_total  INTEGER,
  error        TEXT,
  created_at   TEXT    NOT NULL,
  started_at   TEXT,
  finished_at  TEXT
)
`

// DedupeIndexSQL's predicate writes the three statuses out rather than
// interpolating them from `ActiveStatuses`. What keeps the two in step is
// *behaviour*, not a string comparison: the tests put each status on both sides
// of the boundary, so a status added to one place and not the other fails there.
const DedupeIndexSQL = `
CREATE UNIQUE INDEX IF NOT EXISTS job_active_dedupe
  ON job(adam_id, codec)
  WHERE status IN ('queued', 'waiting', 'running')
`

// DedupeIndexName is the index the dedup path identifies from SQLite's own error
// message.
const DedupeIndexName = "job_active_dedupe"

// BusyTimeoutMS is how long a contended write waits before SQLite gives up. The
// shape of the failure is that the caller sees a busy error rather than a
// silently lost write.
const BusyTimeoutMS = 5000

// StoreError is the database could not be opened or prepared.
//
// Separate from `NotFoundError` because the two need different answers: one is a
// deployment problem to fix before anything works, the other is a caller naming a
// row that is not there. The message names the path, because "unable to open
// database file" on its own does not say which of a container's mounts is
// missing.
type StoreError struct {
	Path  string
	Cause error
}

func (e *StoreError) Error() string {
	return fmt.Sprintf("could not open the job database at %s: %v. The parent directory has "+
		"to exist and be writable -- it belongs on the hub-data volume at /data/hub.db, and a "+
		"missing mount shows up here rather than as an empty queue.", e.Path, e.Cause)
}

func (e *StoreError) Unwrap() error { return e.Cause }

// NotFoundError is a job id that is not in the queue.
//
// Returned rather than ignored: `Mark` on a row that does not exist means the
// caller and the store disagree about the world, and a silent no-op would leave a
// job the UI is still showing as running with nothing to update it.
type NotFoundError struct {
	ID     int64
	Status string
}

func (e *NotFoundError) Error() string {
	return fmt.Sprintf("no job with id %d to mark %s", e.ID, pyrepr.Str(e.Status))
}

// IllegalTransitionError is a status change the queue's own rules forbid -- a
// finished job made active again.
//
// **A distinct type, and not a value error, because the two mean opposite things
// to a caller.** A value error is "you passed something wrong", which is a bug to
// fix at the call site. This is "the world moved on since you read it", which is a
// *race* the caller is expected to handle: the late progress reading in the
// scheduler catches it and drops the reading, which is the correct answer because
// the job it was describing has finished. What it must *not* be is silently
// ignored by `Mark`, because the state it guards is a job with no way out.
type IllegalTransitionError struct {
	ID     int64
	Status string
	Wanted string
}

func (e *IllegalTransitionError) Error() string {
	return fmt.Sprintf("job %d is %s, which is finished, and cannot be marked %s. A job "+
		"becomes active again only by being re-queued, which Mark(%d, \"queued\") does and "+
		"which is what POST /api/jobs/%d/retry calls. If you are holding a progress reading "+
		"for it, the job finished after the reading was taken -- drop the reading rather "+
		"than reviving the row.",
		e.ID, pyrepr.Str(e.Status), pyrepr.Str(e.Wanted), e.ID, e.ID)
}

// Progress is one reading of a transfer, as the ripper reports it.
//
// `Fraction` is nil when the total is unknown, and that is not a degenerate case:
// the wrapper's HLS segments do not always carry a content length, so "bytes so
// far, size unknown" is a real state. nil renders as an indeterminate bar, which
// is true; 0.0 renders as a bar that never moves, which reads as a hang.
type Progress struct {
	BytesDone  int64
	BytesTotal *int64
	Fraction   *float64
}

// Leaf is one track, as the resolver hands it over and the ripper consumes it.
//
// A leaf's `AdamID` and `Codec` are half of the dedup key, which is why every
// field is copied by value and nothing here hands out a reference into a leaf that
// is already enqueued.
//
// `IsMusicVideo` selects the Widevine decryption path over FairPlay and is not
// persisted: the `job` table has no column for it.
type Leaf struct {
	AdamID       string
	Title        string
	AlbumName    string
	ArtistName   string
	Codec        string
	Language     string
	URL          string
	Storefront   string
	IsMusicVideo bool
}

// Job is one row of `job`, as the store reads it back.
//
// `ParentURL` / `ParentType` are the Go-facing names for the `url` / `url_type`
// columns. `ParentID` is the table's self-reference and is nil for everything
// `CreateBatch` writes, because its signature has no parent id. It is here because
// the column is in the schema and `List` filters on it.
//
// `Language` is a plain string although the column is nullable: `CreateBatch` is
// the only writer and it takes the language from a `Leaf`, which has no null. The
// column stays nullable because the DDL declares it without NOT NULL.
type Job struct {
	ID         int64
	ParentID   *int64
	ParentURL  string
	ParentType string
	AdamID     *string
	Title      *string
	Codec      string
	Language   string
	Force      bool
	Status     string
	SkipReason *string
	Progress   *float64
	BytesDone  *int64
	BytesTotal *int64
	Error      *string
	CreatedAt  string
	StartedAt  *string
	FinishedAt *string
}

// BatchResult is what one `CreateBatch` did, per leaf.
//
// `Skipped` is always empty here and that is the design, not an omission: the
// `POST /api/jobs` answer is `{created[], skipped[], deduplicated[]}`, and a track
// already on disk is discovered at **execution** time by the dedup check in the
// scheduler, not at enqueue time. A queued job can sit long enough for the file to
// be deleted underneath it, and a second filesystem check here would put a
// duplicate check with different timing into the codebase for the two to disagree
// about.
type BatchResult struct {
	Created      []int64
	Skipped      []int64
	Deduplicated []int64
}

// RequeueResult is what one `Requeue` did, per row.
//
// `Refused` is not an error list -- those rows are fine, they are simply held by a
// `job_active_dedupe` slot that another job occupies, so they cannot go back to
// `queued` while that job is active. **Reporting them is the whole point**: a
// caller that reported only `Requeued` would show a queue that does not contain
// what the user asked for, and the missing rows would look like a bug in the queue
// rather than a duplicate that is already on its way.
type RequeueResult struct {
	Requeued []int64
	Refused  []int64
}

// ListFilter is `List`'s optional filtering. A zero value means "no filter", which
// is what `GET /api/jobs?status=&parent=` means for an absent query parameter.
type ListFilter struct {
	Status    string
	HasStatus bool
	ParentID  *int64
	ParentURL *string
}

// Store is the queue: enqueue, claim, mark, read, over one SQLite connection.
//
// **One store is one connection, and a second store on the same file is the
// supported shape for a second writer.** `sqlite3` connections are opened
// `FullMutex` by this port, so a single store may be used from several goroutines
// -- and the concurrency test does exactly that, because it is SQLite's write lock
// and not a Go mutex that makes `ClaimNext` exclusive.
//
// **Not a singleton and not a cache.** Every read is a read of the file, and every
// answer is derived from the row that is there now, because the rule is that the
// filesystem is the truth and nothing in this process may hold a second opinion
// about it longer than one statement.
type Store struct {
	conn *sqlite.Conn
	path string
}

// Open connects to `dbPath` and creates the schema if it is absent.
//
// The schema is created on open, so a fresh deployment needs no migration step
// before its first request -- the failure it can have is not being able to open
// the file, and that is reported with the path in it.
func Open(dbPath string) (*Store, error) {
	conn, err := sqlite.Open(dbPath)
	if err != nil {
		return nil, &StoreError{Path: dbPath, Cause: err}
	}
	store := &Store{conn: conn, path: dbPath}
	// `busy_timeout` and `foreign_keys` are per connection, so they are set here
	// for every store rather than once. `journal_mode` is a property of the
	// *file*, so the first connection is enough and the rest inherit it.
	for _, setup := range []string{
		fmt.Sprintf("PRAGMA busy_timeout = %d", BusyTimeoutMS),
		"PRAGMA foreign_keys = ON",
		"PRAGMA journal_mode = WAL",
		JobTableSQL,
		DedupeIndexSQL,
	} {
		if err := conn.Exec(setup); err != nil {
			conn.Close()
			return nil, &StoreError{Path: dbPath, Cause: err}
		}
	}
	return store, nil
}

// Close releases the connection. Idempotent, and safe to leave to the garbage
// collector.
func (s *Store) Close() error {
	if s.conn == nil {
		return nil
	}
	conn := s.conn
	s.conn = nil
	return conn.Close()
}

// Path is the database file this store opened, for a log line that names the
// deployment's mount rather than a generic message.
func (s *Store) Path() string { return s.path }

// CreateBatch enqueues every leaf, and reports which were created and which folded
// into a job.
//
// The two outcomes are disjoint and exhaustive over the leaves: `Created` holds the
// ids of rows this call inserted, and `Deduplicated` holds the ids of the rows that
// already held each key. Nothing is ever silently dropped, and a leaf that arrived
// with no identity is refused rather than being quietly not-created.
//
// A leaf whose key is already held is **not** an error and **not** a new row,
// whatever `force` says. The index has no way to express `force`, which is the
// point: the user asking twice does not mean the track should download twice at
// the same time.
//
// A refusal does not roll the batch back -- the leaves before it are already rows
// -- and the leaves after it are not attempted.
func (s *Store) CreateBatch(parentURL, parentType string, leaves []Leaf, force bool) (BatchResult, error) {
	if !contains(ParentTypes, parentType) {
		return BatchResult{}, fmt.Errorf("parent_type must be one of %s, got %s",
			pyrepr.StrList(ParentTypes), pyrepr.Str(parentType))
	}
	if strings.TrimSpace(parentURL) == "" {
		return BatchResult{}, fmt.Errorf("parent_url is %s, which cannot identify the batch. "+
			"Every job carries it as the `url NOT NULL` column, and the API layer lists and "+
			"cancels by it, so a blank one would make the queue unroutable and unlistable.",
			pyrepr.Str(parentURL))
	}

	result := BatchResult{}
	stamp := now()
	for _, leaf := range leaves {
		if err := checkKey(leaf); err != nil {
			return result, err
		}
		_, lastID, err := s.conn.ExecArgs(
			"INSERT INTO job (url, url_type, adam_id, title, codec, language, force,"+
				" status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
			parentURL, parentType, leaf.AdamID, leaf.Title, leaf.Codec, leaf.Language,
			boolToInt(force), stamp)
		if err != nil {
			// Two independent reasons to re-raise, and both are load-bearing.
			//
			// First: is this the dedup index at all? SQLite names the *columns* of
			// the constraint it refused, so a NOT NULL or CHECK failure on a row
			// the schema rejects for some other reason lands here too. Inferring
			// "so it must be a duplicate" from "a holder exists" is not sound: a
			// different failure on a leaf whose key *is* held would then be
			// reported as deduplicated, and the user would be told their track was
			// queued when no row was written.
			//
			// Second: a holder that cannot be found means the index's own
			// guarantee was not what fired, so re-raise rather than invent an id.
			message, ok := sqlite.ConstraintViolation(err)
			if !ok || !isDedupeViolation(message) {
				return result, err
			}
			holder, holderErr := s.holder(leaf)
			if holderErr != nil {
				return result, holderErr
			}
			if holder == nil {
				return result, err
			}
			result.Deduplicated = append(result.Deduplicated, *holder)
			continue
		}
		result.Created = append(result.Created, lastID)
	}
	return result, nil
}

// checkKey refuses a leaf whose dedup key would mean nothing.
//
// "" would collide with every other "" -- across albums, codecs and URLs -- so
// unrelated tracks would fold into each other. A missing value is the quieter
// failure: SQLite treats every NULL as distinct in a unique index, so a NULL
// `adam_id` neither deduplicates against anything nor prevents a second NULL row,
// and the index looks present while silently not applying. The column is nullable,
// so this is the only place the hole is closed.
func checkKey(leaf Leaf) error {
	for _, field := range []struct {
		name  string
		value string
	}{{"adam_id", leaf.AdamID}, {"codec", leaf.Codec}} {
		if strings.TrimSpace(field.value) == "" {
			return fmt.Errorf("leaf %s is %s, which cannot be part of a dedup key: an empty "+
				"value would collide with every other empty value, and a NULL never collides "+
				"at all in a SQLite unique index. The track is enqueued by nothing here and "+
				"skipped by nothing, which is worse than either.",
				field.name, pyrepr.Str(field.value))
		}
	}
	return nil
}

// holder is the id of the active job holding this leaf's key, or nil if there is
// none.
//
// Runs only after the index has already refused an insert, so at most one row can
// match -- that is the index's own guarantee, and this query cannot return two.
//
// The status filter repeats the index's own predicate and is **not** redundant
// with it. The index constrains what may *exist*; it says nothing about which of
// the several rows that share a key over time this one should return, and a
// `fetchone()`-style lookup takes them in id order. With id 1 `done` and id 2
// `queued` for one key, an unfiltered lookup returns **1** -- the finished job --
// and the UI would then render "already queued as #1" for a job that will never
// run again.
func (s *Store) holder(leaf Leaf) (*int64, error) {
	rows, err := s.conn.Query(
		"SELECT id FROM job WHERE adam_id = ? AND codec = ?"+
			" AND status IN ('queued', 'waiting', 'running')",
		leaf.AdamID, leaf.Codec)
	if err != nil {
		return nil, err
	}
	if rows.Len() == 0 {
		return nil, nil
	}
	id := asInt64(rows.Values[0][0])
	return &id, nil
}

// ClaimNext takes the oldest queued job and marks it running, or nil if there is
// none.
//
// One statement, so it is atomic: a second caller blocks on the write lock for up
// to `busy_timeout` and then re-evaluates the subquery against what the first one
// committed, which is why twelve concurrent claims get twelve different jobs. See
// the package comment for why the read-then-write version of this is not
// equivalent.
//
// `error` is cleared here rather than in `Mark`. A job that has just started has
// no error yet, and leaving the previous run's behind would put a failure message
// next to a `started_at` that is not that run's -- the row describes the current
// run, and `ResumeWaiting` deliberately keeps the reason a job was parked until it
// runs again.
//
// nil rather than a wait: a claim that blocked would stall the scheduler loop, so
// a caller polling a queue another task is draining is told the queue is empty and
// can go round again.
//
// `exclude` is for a pool: ids the caller has already looked at and released on
// this pass, so a job it declined to run is not handed back to it -- or to a
// sibling -- a moment later. Putting a row back and excluding it are both "leave
// it for later", and only one of them terminates: a loop that keeps re-claiming
// the same row never reaches an await point, and in the Python original that stops
// the event loop, the SSE stream and `docker stop`'s grace period with it.
func (s *Store) ClaimNext(exclude []int64) (*Job, error) {
	var rows *sqlite.Rows
	var err error
	if len(exclude) == 0 {
		rows, err = s.conn.Query(
			"UPDATE job SET status = 'running', started_at = ?, error = NULL"+
				" WHERE id = (SELECT id FROM job WHERE status = 'queued' ORDER BY id LIMIT 1)"+
				" RETURNING *", now())
	} else {
		// The placeholders are bound, never interpolated, and the set is a handful
		// of ids at most: a collision needs two active jobs for one `adam_id`, and
		// there are two codecs, so a row can be excluded at most once per claim.
		holes := strings.TrimSuffix(strings.Repeat("?,", len(exclude)), ",")
		args := make([]any, 0, len(exclude)+1)
		args = append(args, now())
		for _, id := range exclude {
			args = append(args, id)
		}
		rows, err = s.conn.Query(
			"UPDATE job SET status = 'running', started_at = ?, error = NULL"+
				" WHERE id = (SELECT id FROM job WHERE status = 'queued'"+
				" AND id NOT IN ("+holes+") ORDER BY id LIMIT 1)"+
				" RETURNING *", args...)
	}
	if err != nil {
		return nil, err
	}
	if rows.Len() == 0 {
		return nil, nil
	}
	return jobFromRow(rows.Columns, rows.Values[0])
}

// Mark sets a job's status, and any of the markable fields passed alongside it.
//
// `finished_at` is not a field: it is set when `status` is terminal and cleared
// when it is not, so that "finished" is always a function of the status and never
// something a caller can leave stale by marking a failed job `queued` for a retry.
// The markable fields are written only when passed, because only the caller knows
// whether it is pausing a transfer or restarting one.
//
// Both the status and the field names are checked against closed sets *before* the
// write, so a misspelled field is an error at the call site rather than a job whose
// progress silently stopped updating.
func (s *Store) Mark(jobID int64, status string, fields MarkFields) error {
	if !contains(AllStatuses, status) {
		return fmt.Errorf("unknown job status %s; expected one of %s",
			pyrepr.Str(status), pyrepr.StrList(AllStatuses))
	}
	assignments := []string{"status = ?", "finished_at = ?"}
	args := []any{status, nil}
	if contains(TerminalStatuses, status) {
		args[1] = now()
	}
	// Fixed order, so the SQL is the same statement for the same call.
	for _, field := range MarkableFields {
		value, present := fields.value(field)
		if !present {
			continue
		}
		assignments = append(assignments, field+" = ?")
		args = append(args, value)
	}
	args = append(args, jobID)

	where := "id = ?"
	// **A finished job cannot become active again except by being re-queued**,
	// which is what a retry does. Any other terminal -> active transition is a
	// caller working from a stale copy of the row, and accepting it is not a small
	// wrong answer: it moves the row out of the terminal set, so delete and retry
	// both start refusing it with a 409 and nothing will ever release it. The job
	// is then stuck *displaying as running* with no user action able to clear it.
	//
	// The case that actually happens is the late progress reading: the ripper's
	// sampler is cancelled in a `finally`, which stops new readings but not one
	// already handed to the event loop, and that callback arrives after the job was
	// marked `done`.
	//
	// **The predicate is in the WHERE clause rather than in a read-then-write.** A
	// `SELECT status` first would be a time-of-check-to-time-of-use gap, and the
	// whole point is that the check and the write must not be separable. The guard
	// costs one indexed comparison on the write that was happening anyway.
	//
	// `ClaimNext` is deliberately *not* subject to this: it is the only legitimate
	// way into `running`, it only ever moves a `queued` row, and it does its own
	// UPDATE rather than going through `Mark`.
	if contains(ActiveStatuses, status) && status != "queued" {
		placeholders := strings.TrimSuffix(strings.Repeat("?,", len(TerminalStatuses)), ",")
		where += " AND status NOT IN (" + placeholders + ")"
		for _, terminal := range sorted(TerminalStatuses) {
			args = append(args, terminal)
		}
	}

	changes, _, err := s.conn.ExecArgs(
		"UPDATE job SET "+strings.Join(assignments, ", ")+" WHERE "+where, args...)
	if err != nil {
		return err
	}
	if changes == 0 {
		// Zero rows is ambiguous between "there is no such job" and "the
		// transition is forbidden", and the two want very different messages. The
		// read happens only on this path, which is never the hot one.
		return s.explainZeroRows(jobID, status)
	}
	return nil
}

func (s *Store) explainZeroRows(jobID int64, status string) error {
	rows, err := s.conn.Query("SELECT status FROM job WHERE id = ?", jobID)
	if err != nil {
		return err
	}
	if rows.Len() == 0 {
		return &NotFoundError{ID: jobID, Status: status}
	}
	current, _ := rows.Values[0][0].(string)
	return &IllegalTransitionError{ID: jobID, Status: current, Wanted: status}
}

// MarkFields carries the four optional columns `Mark` may write.
//
// A struct of pointers rather than a map, so a caller cannot pass a field name
// that is not one -- the closed set that `mark()`'s `**fields` checked at runtime
// is checked by the compiler here. `nil` means "leave it alone", which is why
// every field is a pointer and the zero value writes nothing.
type MarkFields struct {
	Progress   *float64
	BytesDone  *int64
	BytesTotal *int64
	SkipReason *string
	Error      *string
}

func (f MarkFields) value(field string) (any, bool) {
	switch field {
	case "progress":
		return optionalFloat(f.Progress)
	case "bytes_done":
		return optionalInt(f.BytesDone)
	case "bytes_total":
		return optionalInt(f.BytesTotal)
	case "skip_reason":
		return optionalString(f.SkipReason)
	case "error":
		return optionalString(f.Error)
	default:
		panic("jobs: unhandled markable field " + field)
	}
}

// ResumeWaiting moves every `waiting` job back to `queued` and returns how many
// moved.
//
// Called after a successful login: a token expiry parks running jobs in `waiting`
// rather than failing them. The jobs keep their place in the queue, because the
// queue is ordered by `id` and nothing else -- ordering by `created_at` or by
// `started_at`, which a claim has just overwritten, would silently reorder what
// the user asked for. `error` is left alone; a job that does run again keeps the
// reason the queue stopped until it starts, which `ClaimNext` then clears.
func (s *Store) ResumeWaiting() (int64, error) {
	changes, _, err := s.conn.ExecArgs("UPDATE job SET status = 'queued' WHERE status = 'waiting'")
	return changes, err
}

// DeleteFinished removes every row in a terminal status, and returns the ids that
// went.
//
// **The first statement in this project that deletes a row**, and irreversible.
// What it costs is the *record* that a track was attempted -- the title, the error
// and the `skip_reason` evidence paths. It does not cost the file: the filesystem
// is the single source of truth for what is downloaded, and the execution-time
// dedup check reads the library, not this table. So deleting a `done` row cannot
// cause a re-download; it removes a line from a queue log.
//
// A `running` row is never touched: the transfer is in flight and upstream owns its
// partial file. `TerminalStatuses` excludes `running` by construction.
//
// Idempotent, because a UI that double-submits must not be able to turn the second
// click into an error.
//
// **Returns the ids rather than a count** because the caller also holds each job's
// `Leaf` in memory -- the leaf registry has no bulk clear, so only the ids let it
// forget what is no longer queued. A count would leak one entry per deleted row.
func (s *Store) DeleteFinished() ([]int64, error) {
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(TerminalStatuses)), ",")
	args := make([]any, 0, len(TerminalStatuses))
	for _, terminal := range sorted(TerminalStatuses) {
		args = append(args, terminal)
	}
	// Read before write, in one transaction, so the ids cannot drift from the rows
	// deleted: another writer changing a status between the two statements would
	// otherwise produce a list the caller is then told to forget leaves for.
	if err := s.conn.Exec("BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	rows, err := s.conn.Query("SELECT id FROM job WHERE status IN ("+placeholders+")", args...)
	if err != nil {
		_, _, _ = s.conn.ExecArgs("ROLLBACK")
		return nil, err
	}
	doomed := make([]int64, 0, rows.Len())
	for _, row := range rows.Values {
		doomed = append(doomed, asInt64(row[0]))
	}
	if _, _, err := s.conn.ExecArgs("DELETE FROM job WHERE status IN ("+placeholders+")", args...); err != nil {
		_, _, _ = s.conn.ExecArgs("ROLLBACK")
		return nil, err
	}
	if err := s.conn.Exec("COMMIT"); err != nil {
		return nil, err
	}
	sortInts(doomed)
	return doomed, nil
}

// Requeue puts rows in `statuses` back on the queue, and reports the ones that
// could not.
//
// `statuses` is a collection rather than a single status because "re-queue what
// failed" and "re-queue everything that is not done" are both things a user wants,
// and they are the same operation over a different set.
//
// Two guards, and neither is a preference:
//
//   - **A `running` row is never moved.** It is mid-transfer, `job_active_dedupe`
//     holds its key, and `ClaimNext` would hand the same row to a second worker.
//     Callers offering "everything except done" get this exclusion for free.
//   - **A row whose key another job already holds is refused, not raised.** The
//     partial unique index is doing its job; it goes in `Refused` so the caller can
//     tell the user which ones are already on their way.
//
// Each row is a compare-and-set on its *current* status, so a row that moved
// between the read and the write is not clobbered -- the same reason `Mark` is
// careful, and the reason the outcome columns are cleared rather than the row being
// rebuilt.
func (s *Store) Requeue(statuses []string) (RequeueResult, error) {
	// `running` is excluded because moving it would double-rip a track; `queued` is
	// excluded because a row already there is not "requeued" -- nothing happens to
	// it, and counting it would inflate the number a user reads to see whether the
	// button did anything.
	wanted := sorted(uniqueWithout(statuses, "running", "queued"))
	if len(wanted) == 0 {
		return RequeueResult{}, nil
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(wanted)), ",")
	args := make([]any, 0, len(wanted))
	for _, status := range wanted {
		args = append(args, status)
	}
	rows, err := s.conn.Query("SELECT id, status FROM job WHERE status IN ("+placeholders+")", args...)
	if err != nil {
		return RequeueResult{}, err
	}

	result := RequeueResult{}
	for _, row := range rows.Values {
		id := asInt64(row[0])
		status, _ := row[1].(string)
		changes, _, err := s.conn.ExecArgs(
			"UPDATE job SET status = 'queued', error = NULL, skip_reason = NULL,"+
				" started_at = NULL, finished_at = NULL, progress = NULL,"+
				" bytes_done = NULL, bytes_total = NULL"+
				" WHERE id = ? AND status = ?", id, status)
		if err != nil {
			message, ok := sqlite.ConstraintViolation(err)
			if !ok || !isDedupeViolation(message) {
				return result, err
			}
			result.Refused = append(result.Refused, id)
			continue
		}
		if changes > 0 {
			result.Requeued = append(result.Requeued, id)
		} else {
			result.Refused = append(result.Refused, id)
		}
	}
	sortInts(result.Requeued)
	sortInts(result.Refused)
	return result, nil
}

// Get is one job by id, or nil. nil rather than an error, because
// `GET /api/jobs/{id}` for an id the caller made up is a 404 and not a bug in the
// caller.
func (s *Store) Get(jobID int64) (*Job, error) {
	rows, err := s.conn.Query("SELECT * FROM job WHERE id = ?", jobID)
	if err != nil {
		return nil, err
	}
	if rows.Len() == 0 {
		return nil, nil
	}
	return jobFromRow(rows.Columns, rows.Values[0])
}

// List is every job, oldest id first, optionally filtered.
//
// An absent filter means "no filter", which is what `GET /api/jobs?status=&parent=`
// means for an absent query parameter.
//
// `ParentURL` exists for the caller that has to report a *partial* batch:
// `CreateBatch` applies the leaves it could and then fails on one it could not, and
// the handler is required to tell the user what actually landed. There is exactly
// one way to ask that question, and it is not `ParentID`: the column nothing
// writes, whose nil means "no filter" rather than "top level", so filtering a batch
// by it returns the user's **entire** queue and the response names every job ever
// queued as part of this request. The `url` column is what `CreateBatch` wrote for
// every row of a batch, so it is the batch's real identity.
//
// Ordered by `id` and not by `created_at`, so that two jobs enqueued inside one
// millisecond keep the order they were created in, and so that a resumed job goes
// back to the front of the line rather than the back.
func (s *Store) List(filter ListFilter) ([]*Job, error) {
	var clauses []string
	var args []any
	if filter.HasStatus {
		if !contains(AllStatuses, filter.Status) {
			return nil, fmt.Errorf("unknown job status %s; expected one of %s",
				pyrepr.Str(filter.Status), pyrepr.StrList(AllStatuses))
		}
		clauses = append(clauses, "status = ?")
		args = append(args, filter.Status)
	}
	if filter.ParentID != nil {
		clauses = append(clauses, "parent_id = ?")
		args = append(args, *filter.ParentID)
	}
	if filter.ParentURL != nil {
		if strings.TrimSpace(*filter.ParentURL) == "" {
			return nil, fmt.Errorf("parent_url is %s, which cannot be a filter. It is the "+
				"`job` table's `url` column, and CreateBatch refuses a blank one for the same "+
				"reason: matching nothing is not the same as matching every job.",
				pyrepr.Str(*filter.ParentURL))
		}
		clauses = append(clauses, "url = ?")
		args = append(args, *filter.ParentURL)
	}
	query := "SELECT * FROM job"
	if len(clauses) > 0 {
		query += " WHERE " + strings.Join(clauses, " AND ")
	}
	query += " ORDER BY id"

	rows, err := s.conn.Query(query, args...)
	if err != nil {
		return nil, err
	}
	out := make([]*Job, 0, rows.Len())
	for _, row := range rows.Values {
		job, err := jobFromRow(rows.Columns, row)
		if err != nil {
			return nil, err
		}
		out = append(out, job)
	}
	return out, nil
}

// jobFromRow maps one row onto the struct, in the one place the two namings meet.
func jobFromRow(columns []string, values []any) (*Job, error) {
	row := rowMap(columns, values)
	job := &Job{
		ID:         intValue(row, "id"),
		ParentID:   nullableInt(row, "parent_id"),
		ParentURL:  stringValue(row, "url"),
		ParentType: stringValue(row, "url_type"),
		AdamID:     nullableString(row, "adam_id"),
		Title:      nullableString(row, "title"),
		Codec:      stringValue(row, "codec"),
		// The column is nullable and the value is a plain string, exactly as in
		// Python: `CreateBatch` is the only writer and a leaf's language is never
		// null.
		Language:   stringValue(row, "language"),
		Force:      intValue(row, "force") != 0,
		Status:     stringValue(row, "status"),
		SkipReason: nullableString(row, "skip_reason"),
		Progress:   nullableFloat(row, "progress"),
		BytesDone:  nullableInt(row, "bytes_done"),
		BytesTotal: nullableInt(row, "bytes_total"),
		Error:      nullableString(row, "error"),
		CreatedAt:  stringValue(row, "created_at"),
		StartedAt:  nullableString(row, "started_at"),
		FinishedAt: nullableString(row, "finished_at"),
	}
	return job, nil
}

// now is UTC with an offset and millisecond precision.
//
// Milliseconds rather than microseconds because that is exactly what the
// ECMAScript *Date Time String Format* specifies, so `new Date(created_at)` in the
// browser parses it instead of relying on leniency -- and the offset is written as
// `+00:00` rather than `Z`, which is what Python's `datetime.isoformat()` produces
// and therefore what the port has to produce too.
func now() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.000+00:00")
}

// isDedupeViolation reports whether this error is `job_active_dedupe` refusing, and
// not some other constraint.
//
// SQLite reports a violated UNIQUE constraint by naming its **columns**, not its
// index: `UNIQUE constraint failed: job.adam_id, job.codec`. That is a positive
// identification of the rule, taken from the database rather than inferred from the
// absence of something.
//
// The inference it replaces -- "a holder exists, so it must be a duplicate" -- is
// unsound, and the case that breaks it is not exotic: any other integrity failure
// on a leaf whose key is already held arrives here with a holder present, and would
// be reported as "deduplicated", i.e. as a track the user can see queued that has no
// row at all.
//
// Matching the message is a dependence on SQLite's wording, which is worth naming
// as the cost: it has been `UNIQUE constraint failed: <table>.<column>[, ...]` for
// the whole life of the format, and if it ever changes, this returns false for a
// real duplicate -- which turns into a raised error at the call site, i.e. a loud
// failure rather than a silently dropped download.
func isDedupeViolation(message string) bool {
	if !strings.Contains(message, "UNIQUE constraint failed") {
		return false
	}
	return strings.Contains(message, "job.adam_id") && strings.Contains(message, "job.codec")
}

func rowMap(columns []string, values []any) map[string]any {
	out := make(map[string]any, len(columns))
	for i, name := range columns {
		if i < len(values) {
			out[name] = values[i]
		}
	}
	return out
}

func stringValue(row map[string]any, name string) string {
	value, _ := row[name].(string)
	return value
}

func nullableString(row map[string]any, name string) *string {
	value, ok := row[name].(string)
	if !ok {
		return nil
	}
	return &value
}

func intValue(row map[string]any, name string) int64 { return asInt64(row[name]) }

func nullableInt(row map[string]any, name string) *int64 {
	value, ok := row[name].(int64)
	if !ok {
		return nil
	}
	return &value
}

func nullableFloat(row map[string]any, name string) *float64 {
	value, ok := row[name].(float64)
	if !ok {
		return nil
	}
	return &value
}

func asInt64(value any) int64 {
	switch v := value.(type) {
	case int64:
		return v
	case float64:
		return int64(v)
	default:
		return 0
	}
}

func optionalInt(value *int64) (any, bool) {
	if value == nil {
		return nil, false
	}
	return *value, true
}

func optionalFloat(value *float64) (any, bool) {
	if value == nil {
		return nil, false
	}
	return *value, true
}

func optionalString(value *string) (any, bool) {
	if value == nil {
		return nil, false
	}
	return *value, true
}

func boolToInt(value bool) int64 {
	if value {
		return 1
	}
	return 0
}

func contains(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

func concat(first, second []string) []string {
	out := make([]string, 0, len(first)+len(second))
	out = append(out, first...)
	out = append(out, second...)
	return out
}

func uniqueWithout(values []string, drop ...string) []string {
	var out []string
	for _, value := range values {
		if contains(drop, value) || contains(out, value) {
			continue
		}
		out = append(out, value)
	}
	return out
}

// sorted is a code-point sort, which is what Python's `sorted()` is for str.
func sorted(values []string) []string {
	out := append([]string(nil), values...)
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j] < out[j-1]; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}

func sortInts(values []int64) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}

// AsNotFound reports whether an error is a job id that is not in the queue, which
// the API layer answers with a 404 rather than a 500.
func AsNotFound(err error) bool {
	var target *NotFoundError
	return errors.As(err, &target)
}

// AsIllegalTransition reports whether an error is a forbidden status change, which
// the scheduler drops rather than reporting: the job it describes has finished.
func AsIllegalTransition(err error) bool {
	var target *IllegalTransitionError
	return errors.As(err, &target)
}

// SchemaSQL reads `sqlite_master` back as name -> SQL, which is what
// `TestSchemaIsThePythonSchema` compares against the constants above: the
// artifact a human reads in a running database has to be the DDL this package
// says it creates, not a paraphrase of it.
func (s *Store) SchemaSQL() (map[string]string, error) {
	rows, err := s.conn.Query("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
	if err != nil {
		return nil, err
	}
	out := make(map[string]string, rows.Len())
	for _, row := range rows.Values {
		name, _ := row[0].(string)
		sql, _ := row[1].(string)
		out[name] = sql
	}
	return out, nil
}
