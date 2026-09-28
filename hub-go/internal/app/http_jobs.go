package app

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"amdhub/internal/events"
	"amdhub/internal/jobs"
	"amdhub/internal/pyworker"
)

// jobsBody is `POST /api/jobs`'s request body.
type jobsBody struct {
	Urls     []string `json:"urls"`
	Codec    string   `json:"codec"`
	Language string   `json:"language"`
	Force    bool     `json:"force"`
}

// problem is one URL the request could not queue, reported per URL rather than as a
// whole-request failure.
type problem struct {
	URL    string `json:"url"`
	Detail string `json:"detail"`
}

// handleJobsCreate expands each URL and enqueues every track it names.
//
// The whole of the *enqueue* half of dedup. The other half -- "is it already on disk"
// -- is per-file and happens at execution time in the scheduler, so `skipped` is
// always empty here and the response says so by carrying the key.
//
// Every URL is attempted, and one bad URL does not discard the others. A user pasting
// three links and getting an error for the second one should still have the first and
// third queued, so each is expanded in its own attempt and the failures are reported
// per URL.
func (s *State) handleJobsCreate(w http.ResponseWriter, r *http.Request) {
	var body jobsBody
	decodeBody(r, &body)
	status, response := s.createJobsResponse(r.Context(), body)
	writeJSON(w, status, response)
}

// createJobs is the same handler again, for the no-JS form path, which logs the
// problems instead of rendering them.
func (s *State) createJobs(ctx context.Context, body jobsBody) []problem {
	_, response := s.createJobsResponse(ctx, body)
	problems, _ := response["problems"].([]problem)
	return problems
}

// createJobsResponse is the body of the handler, returning the JSON and the status it
// should be sent with.
func (s *State) createJobsResponse(ctx context.Context, body jobsBody) (int, map[string]any) {
	if len(body.Urls) == 0 {
		return http.StatusBadRequest, map[string]any{"detail": "no URLs were given, so " +
			"there is nothing to queue. `urls` is a list of Apple Music links -- a song, " +
			"album, playlist, artist or music video."}
	}
	if !Codecs[body.Codec] {
		return http.StatusBadRequest, map[string]any{"detail": fmt.Sprintf(
			"codec %q is not one this client can rip (%s). A value outside the set would be "+
				"stored in a NOT NULL column and fail once, inside the client's retry loop, "+
				"long after the request that carried it.",
			body.Codec, strings.Join(sortedKeys(Codecs), ", "))}
	}

	worker, err := s.WorkerHandle()
	if err != nil {
		return http.StatusServiceUnavailable, map[string]any{"detail": err.Error()}
	}

	language := strings.TrimSpace(body.Language)
	if language == "" {
		// Asked for rather than defaulted blindly: the library on disk was written with
		// whatever this client was configured with, and metadata in a different language
		// would never match a file name.
		if fromClient, err := worker.RegionLanguage(ctx); err == nil {
			language = strings.TrimSpace(fromClient)
		}
	}
	if language == "" {
		return http.StatusBadRequest, map[string]any{"detail": "no language was given and " +
			"the client's own config does not name one. It is asked for rather than " +
			"defaulted: the library on disk was written with whatever this client was " +
			"configured with, and metadata in a different language would never match a " +
			"file name."}
	}

	created := []int64{}
	skipped := []int64{}
	deduplicated := []int64{}
	rejected := []string{}
	var problems []problem

	for _, url := range body.Urls {
		cleaned := strings.TrimSpace(url)
		leaves, err := worker.Expand(ctx, cleaned, body.Codec, language)
		if err != nil {
			problems = append(problems, problem{URL: url, Detail: clientDetail(err)})
			continue
		}
		if len(leaves) == 0 {
			problems = append(problems, problem{URL: url, Detail: "that URL named no tracks " +
				"-- an empty album, playlist or artist, which is an answer rather than a failure."})
			continue
		}

		before, _ := s.jobsForParent(cleaned)
		thisCreated := []int64{}
		thisDeduplicated := []int64{}

		result, err := s.Store.CreateBatch(cleaned, ParentTypeFor(cleaned, len(leaves)), leaves, body.Force)
		if err != nil {
			// The partial application is the point. `CreateBatch` wrote the leaves before
			// this one and will not attempt the ones after it: what is on disk is not what
			// a full result would have said, and there is no result.
			landedCreated, landedDeduplicated := s.recordLanded(cleaned, before)
			created = append(created, landedCreated...)
			deduplicated = append(deduplicated, landedDeduplicated...)
			rejected = append(rejected, rejectedName(leaves, cleaned))
			problems = append(problems, problem{URL: url, Detail: err.Error()})
		} else {
			created = append(created, result.Created...)
			deduplicated = append(deduplicated, result.Deduplicated...)
			thisCreated = result.Created
			thisDeduplicated = result.Deduplicated
			s.rememberLeaves(leaves, result.Created)
		}

		s.Broker.Publish(events.JobsChannel, events.Message{
			"kind":         "batch",
			"url":          cleaned,
			"created":      intList(thisCreated),
			"deduplicated": intList(thisDeduplicated),
		})
	}

	response := map[string]any{
		"created":      intList(created),
		"skipped":      intList(skipped),
		"deduplicated": intList(deduplicated),
		"rejected":     rejected,
	}
	if len(problems) > 0 {
		// 200 with the detail attached, not 4xx: 19 tracks were queued and a 500 would
		// throw that away. The status is only raised when *nothing* was queued and
		// something went wrong, which is the case where the user has to be told to fix
		// something.
		response["problems"] = problems
		if len(created) == 0 && len(deduplicated) == 0 {
			response["detail"] = problems[0].Detail
			response["url"] = problems[0].URL
			return http.StatusBadRequest, response
		}
	}
	return http.StatusOK, response
}

// clientDetail is the message from a failed expansion, with the wrapper's own wording
// kept where there is one.
func clientDetail(err error) string {
	if clientError, ok := pyworker.AsError(err); ok {
		return clientError.Message
	}
	return err.Error()
}

// jobsForParent is `List(parent_url=...)` as a set, snapshotted before a batch is
// attempted.
//
// The read is by `parent_url` and nothing else: `parent_id` is a self-reference
// nothing writes, so filtering on it would return the user's whole queue as this
// request's work.
func (s *State) jobsForParent(url string) (map[int64]string, error) {
	rows, err := s.Store.List(jobs.ListFilter{ParentURL: &url})
	if err != nil {
		return nil, err
	}
	out := map[int64]string{}
	for _, row := range rows {
		out[row.ID] = row.Status
	}
	return out, nil
}

// recordLanded classifies what `CreateBatch` managed to apply before it failed.
//
// `before` is the ids and statuses this url already had. After the failure there is no
// result object, so this is the only way to know what landed: a row that is not in
// `before` was inserted by this call, and a row that *is* was there because some leaf
// folded into it.
//
// The status check on the second half is not redundant with the snapshot. A row for
// this url can be `done` or `failed`, and the partial index does not cover those -- so
// a new leaf with the same key would have created a *second* row rather than folding,
// and counting the old terminal one as deduplicated would report an id the user never
// queued this time.
func (s *State) recordLanded(url string, before map[int64]string) ([]int64, []int64) {
	var created, deduplicated []int64
	after, err := s.jobsForParent(url)
	if err != nil {
		return nil, nil
	}
	ids := make([]int64, 0, len(after))
	for id := range after {
		ids = append(ids, id)
	}
	sortInt64(ids)
	for _, id := range ids {
		status := after[id]
		if _, seen := before[id]; seen {
			if status == "queued" || status == "waiting" || status == "running" {
				deduplicated = append(deduplicated, id)
			}
			continue
		}
		created = append(created, id)
	}
	return created, deduplicated
}

// rememberLeaves pairs the created ids with their leaves and holds them for the
// scheduler.
//
// Matched on `(adam_id, codec)` -- the store's own dedup key -- because `created` is in
// leaf order but a batch may hold the same key twice, and the second one folded into
// `deduplicated` rather than creating a row. So among the created ids each key is
// unique, and the match is exact rather than positional.
func (s *State) rememberLeaves(leaves []jobs.Leaf, createdIDs []int64) {
	byKey := map[string]jobs.Leaf{}
	for _, leaf := range leaves {
		byKey[leaf.AdamID+"\x00"+leaf.Codec] = leaf
	}
	for _, jobID := range createdIDs {
		job, err := s.Store.Get(jobID)
		if err != nil || job == nil {
			continue
		}
		key := derefString(job.AdamID) + "\x00" + job.Codec
		if leaf, ok := byKey[key]; ok {
			s.Leaves.Put(job.ID, leaf)
		}
	}
}

// rejectedName is a name for the leaf `CreateBatch` refused, so the response is
// actionable.
//
// The refusal is always about a leaf's *dedup key*: `CreateBatch` refuses a bad
// `parent_type`, a blank `parent_url` and an unusable `(adam_id, codec)`, and the first
// two are checked before the call, so an empty or whitespace-only `adam_id` or `codec`
// is the only one that can reach it.
//
// **The offending leaf's own title, not the batch's first one.** A 19-track album is
// not helped by being told track 1 failed, and a title is the only handle a user has on
// a specific track. When the title is blank too, the position and the URL are the
// honest description.
func rejectedName(leaves []jobs.Leaf, url string) string {
	for index, leaf := range leaves {
		if strings.TrimSpace(leaf.AdamID) == "" || strings.TrimSpace(leaf.Codec) == "" {
			if title := strings.TrimSpace(leaf.Title); title != "" {
				return title
			}
			return fmt.Sprintf("track %d of %s", index+1, url)
		}
	}
	return "a track of " + url
}

// handleJobsList is the queue, oldest first, optionally filtered.
//
// `parent` is a *url*, matching what `CreateBatch` wrote. The `parent_id`
// self-reference is populated by nothing, so a filter on it would be a filter on
// nothing.
func (s *State) handleJobsList(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	filter := jobs.ListFilter{}
	if status := query.Get("status"); status != "" {
		filter.Status = status
		filter.HasStatus = true
	}
	if parent := query.Get("parent"); parent != "" {
		filter.ParentURL = &parent
	}
	rows, err := s.Store.List(filter)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": jobDictList(rows, true)})
}

// jobOr404 reads a row and answers 404 itself when there is not one.
//
// **The nil check is the whole reason this exists.** `Store.Get` returns `(nil, nil)` for
// a row that does not exist -- Python's `get()` returning `None`, which is the shape the
// store was ported from -- so a caller that only checks the error dereferences nil, and
// the symptom is a panic in the HTTP handler and a connection closed with no response
// rather than the 404 the client contract promises.
func (s *State) jobOr404(w http.ResponseWriter, jobID int64) (*jobs.Job, bool) {
	job, err := s.Store.Get(jobID)
	if err != nil || job == nil {
		fail(w, http.StatusNotFound, fmt.Sprintf("no job with id %d.", jobID))
		return nil, false
	}
	return job, true
}

// handleJobGet is one row, or a 404 naming the id.
func (s *State) handleJobGet(w http.ResponseWriter, r *http.Request, params map[string]string) {
	jobID, ok := jobIDFrom(params)
	if !ok {
		fail(w, http.StatusBadRequest, fmt.Sprintf("no job with id %s.", params["jobID"]))
		return
	}
	job, ok := s.jobOr404(w, jobID)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, JobDict(job))
}

// handleJobDelete cancels a queued job.
//
// A `running` job is refused rather than raced. There is no way to interrupt a rip that
// is inside the client, so a delete that appeared to work would leave a file being
// written for a row that says `cancelled`. 409 is the honest answer, and it is what
// makes the UI offer "cancel" only where it works.
func (s *State) handleJobDelete(w http.ResponseWriter, r *http.Request, params map[string]string) {
	jobID, ok := jobIDFrom(params)
	if !ok {
		fail(w, http.StatusNotFound, fmt.Sprintf("no job with id %s.", params["jobID"]))
		return
	}
	job, ok := s.jobOr404(w, jobID)
	if !ok {
		return
	}
	if job.Status == "running" {
		fail(w, http.StatusConflict, fmt.Sprintf("job %d is running, and a rip in progress "+
			"cannot be interrupted: the client owns the transfer and its partial file. Wait "+
			"for it to finish, then delete the row.", jobID))
		return
	}
	if err := s.Store.Mark(jobID, "cancelled", jobs.MarkFields{}); err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.Leaves.Forget(jobID)
	s.PublishJob(jobID)
	current, ok := s.jobOr404(w, jobID)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, JobDict(current))
}

// handleJobRetry puts a finished job back on the queue.
//
// A queued, waiting or running job is refused with 409: the partial unique index
// already holds its `(adam_id, codec)`, so re-queueing it is either a no-op or an
// integrity failure, and answering 409 names the real reason.
//
// `error` is deliberately **not** cleared. `ClaimNext` clears it when the job actually
// starts again, so a job the user never retries still says why it failed, and one that
// is retried shows the old reason until the new attempt begins. `finished_at` is
// cleared by `Mark` itself, which is what keeps "finished" a function of the status.
func (s *State) handleJobRetry(w http.ResponseWriter, r *http.Request, params map[string]string) {
	jobID, ok := jobIDFrom(params)
	if !ok {
		fail(w, http.StatusNotFound, fmt.Sprintf("no job with id %s.", params["jobID"]))
		return
	}
	job, ok := s.jobOr404(w, jobID)
	if !ok {
		return
	}
	switch job.Status {
	case "queued", "waiting", "running":
		fail(w, http.StatusConflict, fmt.Sprintf("job %d is %s, not finished, so there is "+
			"nothing to retry.", jobID, job.Status))
		return
	}
	if err := s.Store.Mark(jobID, "queued", jobs.MarkFields{}); err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.PublishJob(jobID)
	current, ok := s.jobOr404(w, jobID)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, JobDict(current))
}

// requeueBody is `POST /api/jobs/requeue`'s body.
type requeueBody struct {
	Scope string `json:"scope"`
}

// handleJobsRequeue puts a set of jobs back on the queue, and says which ones could not
// go back.
//
// **Both lists are in the answer, and that is the contract.** `refused` holds rows
// whose `(adam_id, codec)` another active job already holds -- the index refusing,
// which means the track is already on its way. Reporting them is what lets the UI say
// "2 queued, 1 already running" instead of silently showing a queue that does not
// contain what the user asked for.
func (s *State) handleJobsRequeue(w http.ResponseWriter, r *http.Request) {
	var body requeueBody
	body.Scope = "failed"
	decodeBody(r, &body)
	statuses, known := RequeueScopes[body.Scope]
	if !known {
		fail(w, http.StatusBadRequest, fmt.Sprintf("%q is not a requeue scope. Use one of: %s.",
			body.Scope, strings.Join(RequeueScopeNames(), ", ")))
		return
	}
	result, err := s.Store.Requeue(statuses)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, jobID := range result.Requeued {
		s.PublishJob(jobID)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"requeued": intList(result.Requeued),
		"refused":  intList(result.Refused),
	})
}

// handleJobsDeleteFinished removes every finished row, and forgets the leaves that went
// with them.
//
// **Irreversible, and the answer says how much of it there was.** What is lost is the
// record that a track was attempted. The file is not touched and nothing re-downloads:
// the filesystem is the source of truth for what is on disk.
//
// The leaves are forgotten because the registry is in memory and has no bulk clear:
// without this it would grow by one entry per deleted row for the life of the process.
func (s *State) handleJobsDeleteFinished(w http.ResponseWriter, r *http.Request) {
	deleted, err := s.Store.DeleteFinished()
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, jobID := range deleted {
		s.Leaves.Forget(jobID)
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": len(deleted)})
}

// handleJobsStream is server-sent events: a snapshot, then every change.
//
// Plain `text/event-stream` with one `data:` field per frame. A named `event:` is not
// used: a browser's default `onmessage` handler is the one that has to work without
// configuration, and `EventSource` reconnects on its own when the stream ends -- which
// is what the overrun path below relies on.
func (s *State) handleJobsStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		fail(w, http.StatusInternalServerError, "this server cannot stream.")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	// nginx buffers a proxied response by default, which turns a live stream into one
	// 4 KB block delivered whenever the buffer happens to fill.
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	// A short reconnect delay, as an SSE field rather than as a comment, so a client
	// that drops does not come back in the browser's default 3 s and re-take the
	// snapshot.
	if _, err := fmt.Fprint(w, "retry: 2000\n\n"); err != nil {
		return
	}
	flusher.Flush()

	subscription := s.Broker.Subscribe([]string{events.JobsChannel})[0]
	defer subscription.Close()
	// The snapshot is published *after* the subscription exists, so nothing can fall
	// between registering and the first frame.
	s.Broker.Publish(events.JobsChannel, s.snapshot())

	// **Nothing before the snapshot, and none after.** The broker replays the channel's
	// backlog so a tab opened late renders the current queue -- and that backlog is up
	// to `History` frames of *history*, which here describes a queue that has moved on.
	// Emitting it would render a stale state and then correct it; emitting the snapshot
	// and then the backlog would render it twice. So the first frame out is the
	// `snapshot`, everything before it is dropped, and everything after is delivered
	// untouched.
	seenSnapshot := false
	for {
		// **The context, not a bare `Next`.** A browser tab that closes leaves the
		// request context cancelled and this call returns: without it the handler would
		// block on a channel nobody publishes to, and one dead subscriber would stay
		// subscribed for the life of the process -- the leak `finally: aclose()`
		// prevents on the Python side. The `defer` above then does the removing.
		message, err, ok := subscription.NextContext(r.Context())
		if !ok {
			return
		}
		if err != nil {
			// An overrun ends the response rather than being swallowed. The broker's
			// frames are stale by the time it reports, and the decision belongs here:
			// `EventSource` reconnects, the client gets a fresh snapshot, and nothing is
			// delivered half-out-of-order.
			return
		}
		if !seenSnapshot {
			if kind, _ := message["kind"].(string); kind != "snapshot" {
				continue
			}
			seenSnapshot = true
		}
		if _, err := fmt.Fprint(w, events.Frame(message)); err != nil {
			return
		}
		flusher.Flush()
	}
}

// snapshot is the queue as it is right now, from a real `List`.
//
// **Not a fabricated empty queue.** A tab that connects to a channel nothing has been
// published on would otherwise be handed `{"jobs": []}`, which is indistinguishable
// from a real "the queue is empty" and reads as good news.
func (s *State) snapshot() events.Message {
	rows, err := s.Store.List(jobs.ListFilter{})
	if err != nil {
		return events.Message{"kind": "snapshot", "jobs": []any{}}
	}
	return events.Message{"kind": "snapshot", "jobs": jobDictList(rows, false)}
}

// jobDictList is `[job_to_dict(job) for job in jobs]`, as a JSON-ready slice.
func jobDictList(rows []*jobs.Job, _ bool) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		out = append(out, JobDict(row))
	}
	return out
}

// intList is `[]int64` as a JSON-ready slice, so an empty list serialises as `[]` and
// not as `null` -- which is what `app.js` iterates.
func intList(values []int64) []int64 {
	if values == nil {
		return []int64{}
	}
	return values
}

func sortInt64(values []int64) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}

// jobIDFrom parses the path parameter, which is a string because that is what a URL
// carries.
func jobIDFrom(params map[string]string) (int64, bool) {
	parsed, err := strconv.ParseInt(params["jobID"], 10, 64)
	if err != nil {
		return 0, false
	}
	return parsed, true
}
