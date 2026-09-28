package jobs_test

import (
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"

	"amdhub/internal/jobs"
)

func open(t *testing.T) *jobs.Store {
	t.Helper()
	store, err := jobs.Open(filepath.Join(t.TempDir(), "hub.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func leaf(adam, codec string) jobs.Leaf {
	return jobs.Leaf{
		AdamID: adam, Title: "Title " + adam, AlbumName: "Album", ArtistName: "Artist",
		Codec: codec, Language: "en-US", URL: "https://music.apple.com/jp/album/1",
		Storefront: "jp",
	}
}

func mustCreate(t *testing.T, store *jobs.Store, leaves ...jobs.Leaf) jobs.BatchResult {
	t.Helper()
	result, err := store.CreateBatch("https://music.apple.com/jp/album/1", "album", leaves, false)
	if err != nil {
		t.Fatalf("createBatch: %v", err)
	}
	return result
}

// TestDedupeKeyIsAdamIDAndCodec is the queue's concurrency story in one test: two
// requests for one track are one download, whatever `force` says, and the same
// track in a different codec is a different download.
func TestDedupeKeyIsAdamIDAndCodec(t *testing.T) {
	store := open(t)

	first := mustCreate(t, store, leaf("1", "alac"), leaf("2", "alac"))
	if len(first.Created) != 2 || len(first.Deduplicated) != 0 {
		t.Fatalf("first batch = %+v, want two created", first)
	}

	// The same two keys again, with `force` set: still no second row. The index
	// has no way to express `force` and that is the design -- the user asking
	// twice does not mean the track should download twice at the same time.
	forced, err := store.CreateBatch("https://music.apple.com/jp/album/1", "album",
		[]jobs.Leaf{leaf("1", "alac"), leaf("2", "alac")}, true)
	if err != nil {
		t.Fatalf("forced createBatch: %v", err)
	}
	if len(forced.Created) != 0 {
		t.Fatalf("force created a second row: %+v", forced)
	}
	if len(forced.Deduplicated) != 2 {
		t.Fatalf("forced batch = %+v, want both deduplicated", forced)
	}

	// A different codec is a different key.
	other := mustCreate(t, store, leaf("1", "aac"))
	if len(other.Created) != 1 {
		t.Fatalf("a second codec was folded into the first: %+v", other)
	}

	all, err := store.List(jobs.ListFilter{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(all) != 3 {
		t.Fatalf("queue holds %d rows, want 3", len(all))
	}
}

// TestDeduplicatedNamesTheActiveJobNotTheOldest is the case that made `holder`
// filter on status: with id 1 `done` and id 2 `queued` for one key, an unfiltered
// lookup returns the finished job and the UI renders "already queued as #1" for a
// job that will never run again.
func TestDeduplicatedNamesTheActiveJobNotTheOldest(t *testing.T) {
	store := open(t)
	first := mustCreate(t, store, leaf("1", "alac"))
	if err := store.Mark(first.Created[0], "done", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark done: %v", err)
	}

	second := mustCreate(t, store, leaf("1", "alac"))
	if len(second.Created) != 1 {
		t.Fatalf("a finished job still held the dedupe slot: %+v", second)
	}

	again := mustCreate(t, store, leaf("1", "alac"))
	if len(again.Deduplicated) != 1 || again.Deduplicated[0] != second.Created[0] {
		t.Fatalf("deduplicated = %v, want the running job %d", again.Deduplicated, second.Created[0])
	}
}

// TestEveryActiveStatusHoldsTheDedupeSlot and its terminal counterpart put each
// status on both sides of the boundary. A status added to `ActiveStatuses` without
// being added to the index's predicate fails here.
func TestEveryActiveStatusHoldsTheDedupeSlot(t *testing.T) {
	for _, status := range jobs.ActiveStatuses {
		store := open(t)
		created := mustCreate(t, store, leaf("1", "alac"))
		if status != "queued" {
			if err := store.Mark(created.Created[0], status, jobs.MarkFields{}); err != nil {
				t.Fatalf("mark %s: %v", status, err)
			}
		}
		again := mustCreate(t, store, leaf("1", "alac"))
		if len(again.Deduplicated) != 1 || len(again.Created) != 0 {
			t.Errorf("status %q did not hold the dedupe slot: %+v", status, again)
		}
	}
}

func TestEveryTerminalStatusFreesTheDedupeSlot(t *testing.T) {
	for _, status := range jobs.TerminalStatuses {
		store := open(t)
		created := mustCreate(t, store, leaf("1", "alac"))
		if err := store.Mark(created.Created[0], status, jobs.MarkFields{}); err != nil {
			t.Fatalf("mark %s: %v", status, err)
		}
		again := mustCreate(t, store, leaf("1", "alac"))
		if len(again.Created) != 1 {
			t.Errorf("status %q kept the dedupe slot: %+v", status, again)
		}
	}
}

func TestUnusableKeyIsRefused(t *testing.T) {
	store := open(t)
	for _, bad := range []jobs.Leaf{
		{AdamID: "", Codec: "alac"},
		{AdamID: "   ", Codec: "alac"},
		{AdamID: "1", Codec: ""},
	} {
		if _, err := store.CreateBatch("https://music.apple.com/jp/album/1", "album",
			[]jobs.Leaf{bad}, false); err == nil {
			t.Fatalf("leaf %+v was accepted", bad)
		}
	}
	// A refused leaf does not roll the batch back: the leaves before it are rows.
	result, err := store.CreateBatch("https://music.apple.com/jp/album/1", "album",
		[]jobs.Leaf{leaf("1", "alac"), {AdamID: "", Codec: "alac"}, leaf("2", "alac")}, false)
	if err == nil {
		t.Fatal("a batch with an unusable leaf succeeded")
	}
	if len(result.Created) != 1 {
		t.Fatalf("created = %v, want the leaf before the refusal to have landed", result.Created)
	}
	all, err := store.List(jobs.ListFilter{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(all) != 1 {
		t.Fatalf("queue holds %d rows, want 1: a refused leaf was written", len(all))
	}
}

func TestParentTypeAndURLAreValidated(t *testing.T) {
	store := open(t)
	if _, err := store.CreateBatch("https://x", "albumz", []jobs.Leaf{leaf("1", "alac")}, false); err == nil {
		t.Fatal("an unknown parent_type was accepted")
	}
	for _, blank := range []string{"", "   "} {
		if _, err := store.CreateBatch(blank, "album", []jobs.Leaf{leaf("1", "alac")}, false); err == nil {
			t.Fatalf("parent_url %q was accepted", blank)
		}
	}
	for _, kind := range jobs.ParentTypes {
		if _, err := store.CreateBatch("https://x/"+kind, kind, nil, false); err != nil {
			t.Errorf("parent_type %q refused: %v", kind, err)
		}
	}
}

// TestClaimNextIsExclusiveUnderRealConcurrency is the test a sequential loop
// cannot write. Twelve connections against one file claim at the same time, and
// the SQL's single-statement form is what makes them get twelve different jobs:
// each one blocks on the write lock and then re-evaluates the subquery against
// what the previous claim committed.
func TestClaimNextIsExclusiveUnderRealConcurrency(t *testing.T) {
	path := filepath.Join(t.TempDir(), "hub.db")
	setup, err := jobs.Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer setup.Close()

	const jobCount, workers = 12, 12
	var leaves []jobs.Leaf
	for i := 0; i < jobCount; i++ {
		leaves = append(leaves, leaf(fmt.Sprintf("%d", i), "alac"))
	}
	if _, err := setup.CreateBatch("https://x", "album", leaves, false); err != nil {
		t.Fatalf("createBatch: %v", err)
	}

	claimed := make([]int64, 0, jobCount)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			store, err := jobs.Open(path)
			if err != nil {
				t.Errorf("open worker store: %v", err)
				return
			}
			defer store.Close()
			job, err := store.ClaimNext(nil)
			if err != nil {
				t.Errorf("claimNext: %v", err)
				return
			}
			if job == nil {
				return
			}
			mu.Lock()
			claimed = append(claimed, job.ID)
			mu.Unlock()
		}()
	}
	wg.Wait()

	if len(claimed) != jobCount {
		t.Fatalf("claimed %d jobs, want %d: %v", len(claimed), jobCount, claimed)
	}
	seen := map[int64]bool{}
	for _, id := range claimed {
		if seen[id] {
			t.Fatalf("job %d was claimed twice: %v", id, claimed)
		}
		seen[id] = true
	}
}

func TestClaimNextTakesTheOldestAndClearsTheError(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"), leaf("2", "alac"))
	first, err := store.ClaimNext(nil)
	if err != nil || first == nil {
		t.Fatalf("claimNext: %v %v", first, err)
	}
	if first.ID != batch.Created[0] {
		t.Fatalf("claimed %d, want the oldest %d", first.ID, batch.Created[0])
	}
	if first.Status != "running" || first.StartedAt == nil || first.Error != nil {
		t.Fatalf("claimed row = %+v, want running with a start time and no error", first)
	}

	// `exclude` is what lets a pool put a job back without being handed it again.
	second, err := store.ClaimNext([]int64{first.ID})
	if err != nil || second == nil {
		t.Fatalf("claimNext with exclude: %v %v", second, err)
	}
	if second.ID == first.ID {
		t.Fatal("a claim with the previous job excluded handed out the same row")
	}
	none, err := store.ClaimNext([]int64{first.ID, second.ID})
	if err != nil || none != nil {
		t.Fatalf("claimNext with everything excluded = %+v, %v; want nil", none, err)
	}
}

func TestMarkTerminalSetsFinishedAtAndActiveClearsIt(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	id := batch.Created[0]

	if err := store.Mark(id, "failed", jobs.MarkFields{Error: ptr("boom")}); err != nil {
		t.Fatalf("mark failed: %v", err)
	}
	job, err := store.Get(id)
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.FinishedAt == nil || job.Error == nil || *job.Error != "boom" {
		t.Fatalf("failed row = %+v, want finished_at and the error", job)
	}

	// A retry is `mark(queued)`, which is the one way back to an active status.
	if err := store.Mark(id, "queued", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark queued: %v", err)
	}
	job, err = store.Get(id)
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.FinishedAt != nil {
		t.Fatalf("finished_at survived a re-queue: %+v", job)
	}
	// `error` is deliberately not cleared by `mark`: `ClaimNext` clears it when
	// the job actually starts again, so a job the user never retries still says
	// why it failed.
	if job.Error == nil {
		t.Fatal("mark(queued) cleared the error; only claim_next may do that")
	}
}

// TestMarkRefusesAFinishedJobGoingActive is the race the scheduler has to survive:
// a progress reading handed to the event loop before the job finished arrives
// after it was marked `done`.
func TestMarkRefusesAFinishedJobGoingActive(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	id := batch.Created[0]
	if err := store.Mark(id, "done", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark done: %v", err)
	}
	for _, status := range []string{"running", "waiting"} {
		err := store.Mark(id, status, jobs.MarkFields{})
		if err == nil {
			t.Fatalf("mark(%d, %q) was accepted", id, status)
		}
		if !jobs.AsIllegalTransition(err) {
			t.Fatalf("mark(%d, %q) error = %v, want an IllegalTransitionError", id, status, err)
		}
	}
	// Still terminal, and still releasable: the guard must not have half-applied.
	job, err := store.Get(id)
	if err != nil || job == nil || job.Status != "done" {
		t.Fatalf("row after the refused transitions = %+v (%v)", job, err)
	}
}

func TestMarkReportsAMissingJob(t *testing.T) {
	store := open(t)
	err := store.Mark(4242, "done", jobs.MarkFields{})
	if err == nil {
		t.Fatal("mark on a missing id succeeded")
	}
	if !jobs.AsNotFound(err) {
		t.Fatalf("error = %v, want a NotFoundError", err)
	}
}

func TestMarkValidatesItsInputs(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	if err := store.Mark(batch.Created[0], "finished", jobs.MarkFields{}); err == nil {
		t.Fatal("an unknown status was accepted")
	}
	// A misspelled field is not expressible here: `MarkFields` is a struct of
	// pointers, so the closed set Python checked at runtime is checked by the
	// compiler. What is still runtime is a nil field, which must not be written:
	// a nil `Error` leaves the column alone rather than nulling it.
	if err := store.Mark(batch.Created[0], "running", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark with no fields: %v", err)
	}
	job, err := store.Get(batch.Created[0])
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.Progress != nil || job.BytesDone != nil || job.SkipReason != nil {
		t.Fatalf("a field nobody passed was written: %+v", job)
	}
}

func TestProgressAndSkipReasonRoundTrip(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	id := batch.Created[0]
	fraction := 0.5
	err := store.Mark(id, "skipped", jobs.MarkFields{
		Progress:   &fraction,
		BytesDone:  ptrInt(1024),
		BytesTotal: ptrInt(2048),
		SkipReason: ptr("already on disk: /library/Artist/Album/01. Title.m4a"),
	})
	if err != nil {
		t.Fatalf("mark: %v", err)
	}
	job, err := store.Get(id)
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.Progress == nil || *job.Progress != 0.5 ||
		job.BytesDone == nil || *job.BytesDone != 1024 ||
		job.BytesTotal == nil || *job.BytesTotal != 2048 ||
		job.SkipReason == nil || !strings.Contains(*job.SkipReason, "/library/Artist/Album/") {
		t.Fatalf("round trip lost a value: %+v", job)
	}
	// A reading with no total is a real state -- HLS segments do not always carry
	// a content length -- and it must survive as nil rather than becoming 0. A
	// fresh row, because `mark` writes only the fields it is given: the value the
	// previous call set is deliberately still there.
	fresh := mustCreate(t, store, leaf("2", "alac"))
	if err := store.Mark(fresh.Created[0], "queued", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark queued: %v", err)
	}
	claimed, err := store.ClaimNext(nil)
	if err != nil || claimed == nil {
		t.Fatalf("claimNext: %v %v", claimed, err)
	}
	if err := store.Mark(claimed.ID, "running", jobs.MarkFields{BytesDone: ptrInt(7)}); err != nil {
		t.Fatalf("mark running: %v", err)
	}
	job, err = store.Get(claimed.ID)
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.BytesTotal != nil {
		t.Fatalf("an unknown total became a number: %+v", *job.BytesTotal)
	}
	if job.BytesDone == nil || *job.BytesDone != 7 {
		t.Fatalf("bytes_done = %v, want 7", job.BytesDone)
	}
}

func TestResumeWaitingKeepsTheQueueOrderAndTheReason(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"), leaf("2", "alac"))
	for _, id := range batch.Created {
		// A token expiry parks a *running* job in `waiting`; parked jobs are not
		// claimable, which is what stops them being re-run alongside a new one.
		if err := store.Mark(id, "running", jobs.MarkFields{}); err != nil {
			t.Fatalf("mark running: %v", err)
		}
		if err := store.Mark(id, "waiting", jobs.MarkFields{
			Error: ptr("apple token expired"),
		}); err != nil {
			t.Fatalf("mark waiting: %v", err)
		}
	}
	if job, err := store.ClaimNext(nil); err != nil || job != nil {
		t.Fatalf("a waiting job was claimable: %+v %v", job, err)
	}

	moved, err := store.ResumeWaiting()
	if err != nil {
		t.Fatalf("resumeWaiting: %v", err)
	}
	if moved != 2 {
		t.Fatalf("resumeWaiting moved %d, want 2", moved)
	}
	job, err := store.ClaimNext(nil)
	if err != nil || job == nil {
		t.Fatalf("claim after resume: %+v %v", job, err)
	}
	if job.ID != batch.Created[0] {
		t.Fatalf("resumed job %d jumped the queue ahead of %d", job.ID, batch.Created[0])
	}
	if job.Error != nil {
		// The reason is kept until the job runs again, and then it is cleared by
		// the claim itself: the row describes the current run.
		t.Fatalf("the claim did not clear the parked reason: %+v", job)
	}
}

func TestDeleteFinishedKeepsRunningAndReturnsIds(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"), leaf("2", "alac"), leaf("3", "alac"))
	running := batch.Created[0]
	if _, err := store.ClaimNext(nil); err != nil {
		t.Fatalf("claimNext: %v", err)
	}
	if err := store.Mark(batch.Created[1], "done", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark done: %v", err)
	}
	if err := store.Mark(batch.Created[2], "failed", jobs.MarkFields{Error: ptr("no")}); err != nil {
		t.Fatalf("mark failed: %v", err)
	}

	deleted, err := store.DeleteFinished()
	if err != nil {
		t.Fatalf("deleteFinished: %v", err)
	}
	if len(deleted) != 2 || deleted[0] != batch.Created[1] || deleted[1] != batch.Created[2] {
		t.Fatalf("deleted = %v, want the two terminal ids sorted", deleted)
	}
	all, err := store.List(jobs.ListFilter{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(all) != 1 || all[0].ID != running {
		t.Fatalf("queue = %+v, want only the running job %d", all, running)
	}
	// Idempotent: a UI that double-submits must not turn the second click into an
	// error.
	again, err := store.DeleteFinished()
	if err != nil || len(again) != 0 {
		t.Fatalf("second deleteFinished = %v, %v", again, err)
	}
}

func TestRequeueRefusesRunningAndHeldKeys(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	if err := store.Mark(batch.Created[0], "failed", jobs.MarkFields{Error: ptr("boom")}); err != nil {
		t.Fatalf("mark failed: %v", err)
	}
	// A failed row frees its key, so the same track can be queued again -- which
	// is how two rows for one key exist at all, and the case this test needs.
	retry := mustCreate(t, store, leaf("1", "alac"))
	if len(retry.Created) != 1 {
		t.Fatalf("a failed job still held the dedupe slot: %+v", retry)
	}
	if _, err := store.ClaimNext(nil); err != nil {
		t.Fatalf("claimNext: %v", err)
	}

	result, err := store.Requeue([]string{"failed", "running", "queued", "done"})
	if err != nil {
		t.Fatalf("requeue: %v", err)
	}
	// The `running` row is excluded outright -- moving it would double-rip the
	// track -- and the failed one is refused because the running job now holds its
	// key. Both are reported rather than raised: the caller has to tell the user
	// which ones are already on their way.
	if len(result.Requeued) != 0 {
		t.Fatalf("requeued = %v, want nothing", result.Requeued)
	}
	if len(result.Refused) != 1 || result.Refused[0] != batch.Created[0] {
		t.Fatalf("refused = %v, want the failed row %d", result.Refused, batch.Created[0])
	}

	// With the holder gone, the same row goes back cleanly, and re-queuing clears
	// the outcome columns rather than leaving them next to a fresh start.
	if err := store.Mark(retry.Created[0], "cancelled", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark cancelled: %v", err)
	}
	result, err = store.Requeue([]string{"failed", "cancelled"})
	if err != nil {
		t.Fatalf("requeue: %v", err)
	}
	if len(result.Requeued) != 1 || result.Requeued[0] != batch.Created[0] {
		t.Fatalf("requeued = %v, want %d", result.Requeued, batch.Created[0])
	}
	job, err := store.Get(batch.Created[0])
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	if job.Status != "queued" || job.Error != nil || job.StartedAt != nil || job.FinishedAt != nil {
		t.Fatalf("re-queued row = %+v, want a clean queued row", job)
	}
}

func TestListFiltersAndOrder(t *testing.T) {
	store := open(t)
	first, err := store.CreateBatch("https://x/album/1", "album",
		[]jobs.Leaf{leaf("1", "alac"), leaf("2", "alac")}, false)
	if err != nil {
		t.Fatalf("createBatch: %v", err)
	}
	second, err := store.CreateBatch("https://x/album/2", "album", []jobs.Leaf{leaf("3", "alac")}, false)
	if err != nil {
		t.Fatalf("createBatch: %v", err)
	}
	if err := store.Mark(first.Created[0], "done", jobs.MarkFields{}); err != nil {
		t.Fatalf("mark: %v", err)
	}

	done, err := store.List(jobs.ListFilter{Status: "done", HasStatus: true})
	if err != nil || len(done) != 1 || done[0].ID != first.Created[0] {
		t.Fatalf("status filter = %+v (%v)", done, err)
	}
	byURL, err := store.List(jobs.ListFilter{ParentURL: &[]string{"https://x/album/2"}[0]})
	if err != nil || len(byURL) != 1 || byURL[0].ID != second.Created[0] {
		t.Fatalf("parent_url filter = %+v (%v)", byURL, err)
	}
	all, err := store.List(jobs.ListFilter{})
	if err != nil || len(all) != 3 {
		t.Fatalf("unfiltered list = %d rows (%v)", len(all), err)
	}
	// Ordered by id, which is what keeps a resumed job at the front of the line.
	for i := 1; i < len(all); i++ {
		if all[i].ID < all[i-1].ID {
			t.Fatalf("list is not id-ordered: %+v", all)
		}
	}
	if _, err := store.List(jobs.ListFilter{Status: "finished", HasStatus: true}); err == nil {
		t.Fatal("an unknown status filter was accepted")
	}
	if _, err := store.List(jobs.ListFilter{ParentURL: ptr(" ")}); err == nil {
		t.Fatal("a blank parent_url filter was accepted")
	}
}

// TestSchemaIsThePythonSchema reads the DDL back out of `sqlite_master` and
// compares the index predicate to `ActiveStatuses` as a set. This is the check
// that catches a status added to one place and not the other *after the fact* --
// the behavioural tests above catch it on the way in.
func TestSchemaIsThePythonSchema(t *testing.T) {
	store := open(t)
	all, err := store.List(jobs.ListFilter{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(all) != 0 {
		t.Fatalf("a fresh store is not empty: %+v", all)
	}

	schema, err := store.SchemaSQL()
	if err != nil {
		t.Fatalf("schema: %v", err)
	}
	index, ok := schema["job_active_dedupe"]
	if !ok {
		t.Fatalf("job_active_dedupe is not in sqlite_master: %v", schema)
	}
	wanted := "status IN ('queued', 'waiting', 'running')"
	if !strings.Contains(strings.Join(strings.Fields(index), " "), wanted) {
		t.Fatalf("index predicate = %q, want it to contain %q", index, wanted)
	}
	if normalizeDDL(index) != normalizeDDL(jobs.DedupeIndexSQL) {
		t.Fatalf("index DDL drifted from DedupeIndexSQL:\n%s", index)
	}
	table, ok := schema["job"]
	if !ok {
		t.Fatalf("the job table is not in sqlite_master: %v", schema)
	}
	if normalizeDDL(table) != normalizeDDL(jobs.JobTableSQL) {
		t.Fatalf("table DDL drifted from JobTableSQL:\n%s", table)
	}
}

// TestCreatedAtIsECMAScriptParseable pins the one timestamp the browser parses:
// milliseconds and an explicit offset, so `new Date(created_at)` is not relying on
// leniency.
func TestCreatedAtIsECMAScriptParseable(t *testing.T) {
	store := open(t)
	batch := mustCreate(t, store, leaf("1", "alac"))
	job, err := store.Get(batch.Created[0])
	if err != nil || job == nil {
		t.Fatalf("get: %v %v", job, err)
	}
	shape := regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$`)
	if !shape.MatchString(job.CreatedAt) {
		t.Fatalf("created_at = %q, want an ECMAScript Date Time String Format value", job.CreatedAt)
	}
}

func TestOpenFailureNamesThePath(t *testing.T) {
	_, err := jobs.Open(filepath.Join(t.TempDir(), "missing-dir", "hub.db"))
	if err == nil {
		t.Fatal("opening a database under a missing directory succeeded")
	}
	var storeErr *jobs.StoreError
	if !errors.As(err, &storeErr) {
		t.Fatalf("error = %T (%v), want a StoreError", err, err)
	}
	if !strings.Contains(err.Error(), "missing-dir") {
		t.Fatalf("error does not name the path: %v", err)
	}
	if !strings.Contains(err.Error(), "/data/hub.db") {
		t.Fatalf("error does not say where the database belongs: %v", err)
	}
}

// normalizeDDL compares a DDL statement to the constant it came from. SQLite
// stores the text it was given with `IF NOT EXISTS` removed -- the clause is
// evaluated at creation and is not part of the schema -- so that one phrase is
// dropped here rather than duplicated in the constant.
func normalizeDDL(sql string) string {
	return strings.Join(strings.Fields(strings.ReplaceAll(sql, "IF NOT EXISTS ", "")), " ")
}

func ptr(s string) *string { return &s }

func ptrInt(v int64) *int64 { return &v }
