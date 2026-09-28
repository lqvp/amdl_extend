package app

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"amdhub/internal/dedup"
	"amdhub/internal/events"
	"amdhub/internal/jobs"
	"amdhub/internal/library"
	"amdhub/internal/pyworker"
)

// SchedulerLoop claims, runs, sleeps; until the process is asked to stop. It is the
// port of `app.scheduler_loop`.
//
// **The readiness probe happens immediately before every claim, not on a timer.**
// That is what replaced a 2 Hz poll: the question "is the wrapper ready right now?" is
// only interesting immediately before acting on the answer, and asking it any other
// time is a request whose only consumer is a sleep. So the loop is
//
//	probe -> claim -> run -> sleep
//
// and the probe is one uncached status read per iteration *that reaches the claim*,
// which for an idle hub is one per `IdleReadinessPollSeconds` rather than two per
// second.
//
// The two quiet states are then both right: a wrapper that is not running leaves the
// queue `queued` (so the user's order is intact and `POST /api/wrapper/start` makes it
// run), and a wrapper serving with no account says so once instead of failing every
// job with an error about `/key`.
func (s *State) SchedulerLoop(ctx context.Context) {
	announced := ""
	for {
		if s.Stopping() || ctx.Err() != nil {
			return
		}
		// A SQLite read, not an HTTP request, and that distinction is the whole fix:
		// an idle hub -- no `queued` and no `waiting` job -- is the only state in which
		// readiness cannot matter.
		if !s.hasActionable() {
			s.sleep(ctx, IdlePollSeconds)
			continue
		}

		problem := s.wrapperProblem(ctx)
		s.setCachedProblem(problem)
		if problem != "" {
			if problem != announced {
				announced = problem
				s.Broker.Publish(events.JobsChannel, events.Message{
					"kind": "wrapper", "problem": problem,
				})
			}
			// The wrapper is not serving, so poll at the *readiness* rate: the thing
			// being waited on is the wrapper coming back, and there is nothing to gain
			// from looking for new work twice a second.
			s.sleep(ctx, IdleReadinessPollSeconds)
			continue
		}

		// **The wrapper can serve, so every parked job goes back on the queue.**
		//
		// Unconditional on every ready probe, not keyed on a transition. A transition
		// guard sounds tighter and is not: a token that dies and recovers between two
		// probes is never observed unready, a hub that restarts holding parked jobs sees
		// only ready answers, and a crash the supervisor fixes inside one poll is the
		// same case. All three leave a `waiting` job that no transition can release.
		// The cost of calling it anyway is one indexed `UPDATE ... WHERE status =
		// 'waiting'` that matches zero rows.
		if resumed, err := s.Store.ResumeWaiting(); err == nil && resumed > 0 {
			s.logLine(fmt.Sprintf("the wrapper is serving again; %d parked job(s) requeued", resumed))
		}
		announced = ""

		ran, err := s.RunPool(ctx)
		if err != nil {
			// A worker that cannot claim is not a reason to stop the loop: the next
			// iteration reports the wrapper's state and tries again.
			s.logLine(fmt.Sprintf("the queue could not be run: %v", err))
			s.sleep(ctx, IdlePollSeconds)
			continue
		}
		// A job ran, so the wrapper was ready moments ago; a change since then is more
		// likely to be the kind worth noticing.
		if ran > 0 {
			s.sleep(ctx, PostJobPollSeconds)
		} else {
			s.sleep(ctx, IdlePollSeconds)
		}
	}
}

// hasActionable reports whether the loop has anything to do at all: a `queued` or a
// `waiting` job.
//
// Two statuses, and **`waiting` is the one that is easy to miss.** A queue whose only
// contents are parked jobs still needs the loop running, because the loop is the only
// thing that probes readiness and readiness is what releases them. With `queued`
// alone, a hub that came up with three parked jobs and a healthy wrapper would spin
// asking SQLite, never probe, and never resume.
func (s *State) hasActionable() bool {
	for _, status := range []string{"queued", "waiting"} {
		found, err := s.Store.List(jobs.ListFilter{Status: status, HasStatus: true})
		if err != nil {
			// A store that cannot be read is not an idle queue: take the slow path, where
			// the claim fails loudly instead of the hub going quiet.
			return true
		}
		if len(found) > 0 {
			return true
		}
	}
	return false
}

// wrapperProblem is "" when a download could run, else "no-account", "unreachable" or
// "unavailable".
//
// The supervisor's own uncached status, not a cached copy, because the whole point is
// that a *change* -- the token expiring while the hub runs -- is a state change a
// cache would hide.
func (s *State) wrapperProblem(ctx context.Context) string {
	if !s.Supervisor.Running() {
		return "unavailable"
	}
	status, err := s.Supervisor.Status(ctx)
	if err != nil {
		return "unreachable"
	}
	regions, _ := status["regions"].([]any)
	if len(regions) == 0 {
		return "no-account"
	}
	return ""
}

func (s *State) setCachedProblem(problem string) {
	s.mu.Lock()
	s.cachedProblem = problem
	s.mu.Unlock()
}

func (s *State) sleep(ctx context.Context, duration time.Duration) {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-s.Stopped():
	case <-ctx.Done():
	}
}

// poolState is what the workers of one pass share: which tracks are in flight, and
// which rows this pass has decided not to run.
type poolState struct {
	mu       sync.Mutex
	running  map[string]int64 // adam_id -> job id
	declined map[int64]bool
}

func newPoolState() *poolState {
	return &poolState{running: map[string]int64{}, declined: map[int64]bool{}}
}

func (p *poolState) decline(jobID int64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.declined[jobID] = true
}

func (p *poolState) isDeclined(jobID int64) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.declined[jobID]
}

func (p *poolState) excluded() []int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]int64, 0, len(p.declined))
	for id := range p.declined {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (p *poolState) holder(adamID string) int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.running[adamID]
}

func (p *poolState) claim(adamID string, jobID int64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.running[adamID] = jobID
}

func (p *poolState) release(adamID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.running, adamID)
}

// RunPool runs the queue through `RipConcurrency` workers, and reports how many jobs
// it ran.
//
// **Why more than one.** A 41.8 MB ALAC track takes 9.8 s, and 6.1 s of that is the
// wrapper answering `/lyrics`, the album lookup and the codec check before a byte of
// audio moves. The audio then crosses at ~41 MB/s in about a second. So 85% of a
// track's wall clock is an API round-trip, and a serial queue spent almost all of its
// time waiting for a sibling that was not running.
//
// **The collision this makes reachable, and why the scheduler owns the guard.** The
// queue's unique index is `(adam_id, codec)`, so the same track in `alac` and in `aac`
// is two legal active jobs. But the client short-circuits on `adam_id` alone, so
// whichever of the two starts second finds the first in its table, returns
// immediately, and the hub marks it `done` with nothing downloaded. Serially that
// could not happen. So a job whose `adam_id` is already in flight is not run: it is
// released and the claim excludes it for the rest of the pass.
//
// Every worker is waited for before the first error is returned, so one worker's
// failure cannot strand its siblings mid-rip with nobody reading them.
func (s *State) RunPool(ctx context.Context) (int, error) {
	limit := s.Settings.RipConcurrency
	if limit < 1 {
		limit = 1
	}
	shared := newPoolState()
	type outcome struct {
		ran int
		err error
	}
	results := make(chan outcome, limit)
	for index := 0; index < limit; index++ {
		go func() {
			ran, err := s.worker(ctx, shared, -1)
			results <- outcome{ran: ran, err: err}
		}()
	}
	total := 0
	var firstError error
	for index := 0; index < limit; index++ {
		outcome := <-results
		total += outcome.ran
		if outcome.err != nil && firstError == nil {
			firstError = outcome.err
		}
	}
	return total, firstError
}

// RunOne claims and runs at most one job, and reports whether it did.
//
// Split out of the loop so that a caller -- a test, or an operator's one-shot -- can
// drive exactly one step without a background goroutine racing it. A single worker
// with a budget of one, so there is one implementation of "claim, decide, run, mark"
// and not two.
func (s *State) RunOne(ctx context.Context) (bool, error) {
	ran, err := s.worker(ctx, newPoolState(), 1)
	return ran > 0, err
}

// worker claims and runs jobs until there is nothing left to claim, or the budget is
// spent. A budget below zero means "no bound".
//
// One worker, looping -- rather than a pass that claims N and waits for the slowest --
// so a slot that frees is refilled immediately. That is the difference the music-video
// case turns on: a five-minute video in a 4-slot *pass* holds three other slots idle
// for five minutes, which is three tracks not started that could each have been
// finished in ten seconds.
//
// `budget` counts jobs actually *run*, not rows looked at, so a pass over a queue it
// had to defer does not spend its budget.
func (s *State) worker(ctx context.Context, shared *poolState, budget int) (int, error) {
	ran := 0
	for budget < 0 || ran < budget {
		if ctx.Err() != nil || s.Stopping() {
			return ran, nil
		}
		// `exclude`, not "claim it and put it back". Releasing a row makes it the lowest
		// eligible id again, so the next claim hands it straight back -- and in the
		// Python original that loop was synchronous throughout, so the event loop
		// stopped entirely: not a slow queue but a hub that answers no request, streams
		// no progress and ignores `docker stop`. Excluding hands back the *next*
		// eligible row, or nothing, which is an answer a loop can act on.
		job, err := s.Store.ClaimNext(shared.excluded())
		if err != nil {
			return ran, err
		}
		if job == nil {
			return ran, nil
		}
		if shared.isDeclined(job.ID) {
			// Unreachable while `exclude` works, and kept anyway because the alternative
			// is a claim loop that never sleeps.
			_ = s.Store.Mark(job.ID, "queued", jobs.MarkFields{})
			return ran, nil
		}
		adamID := derefString(job.AdamID)
		if adamID != "" && shared.holder(adamID) != job.ID {
			// Upstream's own guard, one layer up: the same track in two codecs has to be
			// ripped one after the other.
			shared.decline(job.ID)
			_ = s.Store.Mark(job.ID, "queued", jobs.MarkFields{})
			continue
		}
		if adamID != "" {
			shared.claim(adamID, job.ID)
		}
		ran++

		runErr := s.execute(ctx, job)

		// Released here rather than by a `defer` in the loop, which would hold every
		// track of a long pass as "in flight" until the worker returned -- and would
		// then decline legitimate work.
		if adamID != "" {
			shared.release(adamID)
		}
		s.Leaves.Forget(job.ID)

		if runErr != nil {
			// One job's failure is not the loop's. A shutdown is the exception: the row
			// is marked `cancelled` with the reason rather than left `running` for a
			// process that is going away.
			if errors.Is(runErr, context.Canceled) || s.Stopping() {
				s.mark(job, "cancelled", jobs.MarkFields{
					Error: ptr("the hub shut down while this was running"),
				})
				return ran, nil
			}
			s.mark(job, "failed", jobs.MarkFields{
				Error: ptr(fmt.Sprintf("%s: %v", errorName(runErr), runErr)),
			})
		}
	}
	return ran, nil
}

// execute is one job: find its leaf, decide whether it is on disk, and rip it or skip
// it. The port of `app._execute`.
func (s *State) execute(ctx context.Context, job *jobs.Job) error {
	leaf, err := s.leafFor(ctx, job)
	if err != nil {
		return err
	}
	if leaf == nil {
		s.mark(job, "failed", jobs.MarkFields{Error: ptr(fmt.Sprintf(
			"adam_id=%s could not be expanded from %s any more, so there is no album name, "+
				"artist or storefront to rip it with. The `job` table does not store them, so "+
				"the only way to know them is to ask the catalogue -- and it no longer lists "+
				"this track under that URL. Re-submit the URL to queue it again.",
			derefString(job.AdamID), job.ParentURL))})
		return nil
	}

	if !job.Force && !leaf.IsMusicVideo {
		// A deliberate non-goal: a music video lives in one flat directory, so there is
		// no album scope to compare against and it is always re-downloaded. Checking it
		// would either never match or match the wrong thing, and the user must not be
		// told a video was "already downloaded".
		hit, err := s.filesystemDuplicate(ctx, leaf)
		if err != nil {
			return err
		}
		if hit != nil {
			s.mark(job, "skipped", jobs.MarkFields{SkipReason: ptr(SkipReason(hit))})
			return nil
		}
	}

	worker, err := s.WorkerHandle()
	if err != nil {
		return err
	}
	// Set for the duration of the rip and cleared afterwards. It used to be set and left
	// set in Python, which meant the *next* job's first reading -- and any stray reading
	// from a sampler that outlived its rip -- was written to a job id that had nothing
	// to do with it.
	s.setCurrentJob(&job.ID)
	if leaf.IsMusicVideo {
		err = worker.RunMusicVideo(ctx, job.ID, *leaf, job.Force)
	} else {
		err = worker.RunSong(ctx, job.ID, *leaf, job.Force)
	}
	s.setCurrentJob(nil)

	if err != nil {
		// A wrapper that stops serving *during* a rip parks the job in `waiting` rather
		// than failing it. The discriminator is the wrapper's own observable state, never
		// the message, and the reason decides the message because the ways out differ: a
		// crash is recovered by the supervisor or by a start, an account that signed out
		// needs a human, and an unanswerable health check needs neither.
		if _, isClientFailure := pyworker.AsError(err); isClientFailure {
			reason, detail := s.parkReason(ctx)
			if reason != "" {
				s.mark(job, "waiting", jobs.MarkFields{Error: ptr(parkMessage(reason, err, detail))})
				return nil
			}
		}
		return err
	}
	s.mark(job, "done", jobs.MarkFields{})
	return nil
}

// parkReason is why the wrapper cannot serve a download right now, and what the probe
// said about it. The port of `app._park_reason`.
//
// **The discriminator is the wrapper's observable state, not the error's prose.**
// Matching on a substring of a message is exactly the kind of test that dies quietly:
// upstream rewords a log line and the queue starts failing jobs that should have
// waited, with no failing test. So the question asked is `regions`.
//
// A failed probe is still `not ready` for parking purposes: unknown is not ready, and
// `waiting` is re-checked before the next claim, so the safe direction is to wait.
func (s *State) parkReason(ctx context.Context) (string, string) {
	if !s.Supervisor.Running() {
		return "unavailable", ""
	}
	status, err := s.Supervisor.Status(ctx)
	if err != nil {
		return "unreachable", fmt.Sprintf("%s: %v", errorName(err), err)
	}
	regions, _ := status["regions"].([]any)
	if len(regions) == 0 {
		return "no-account", ""
	}
	return "", ""
}

// parkMessage is `PARK_MESSAGES`, keyed by reason so a message cannot be attached to
// the wrong cause.
//
// **Each message states only what was observed.** `unavailable` is only reached when
// the supervisor's own record says it has no process, so "the wrapper stopped serving"
// is a reading of evidence rather than an inference -- which is why `unreachable` is a
// separate case rather than a softer version of the same sentence.
func parkMessage(reason string, cause error, detail string) string {
	switch reason {
	case "no-account":
		return fmt.Sprintf("the Apple account signed out while this was downloading: %v Log "+
			"in from the queue page and it resumes from here.", cause)
	case "unavailable":
		return fmt.Sprintf("the wrapper stopped serving while this was downloading: %v This "+
			"resumes on its own as soon as the wrapper is back -- nothing to log in to, and no "+
			"action needed unless the wrapper does not come back.", cause)
	default:
		return fmt.Sprintf("the check that says whether the wrapper can serve failed while "+
			"this was downloading, so it is not known what the wrapper was doing: %s The "+
			"download itself ended with %v The wrapper may well be fine; this resumes on its "+
			"own as soon as the check succeeds again.", detail, cause)
	}
}

// leafFor is the leaf for a job, or nil when it cannot be described any more.
//
// Two sources, in order. The registry holds the expansion this process made, which is
// the common case and free. The second is a re-expansion of the parent URL, which is
// what makes a restart survivable: a queued row outlives the process that wrote it,
// and the `job` table does not carry the fields the rip needs.
//
// Matching is on `(adam_id, codec)` and not on `adam_id` alone, because a playlist
// that lists a track twice, or an artist whose catalogue holds the same song in two
// codecs, is an ordinary shape and a job must be ripped in the codec it was queued for.
func (s *State) leafFor(ctx context.Context, job *jobs.Job) (*jobs.Leaf, error) {
	if leaf, ok := s.Leaves.Get(job.ID); ok {
		return &leaf, nil
	}
	worker, err := s.WorkerHandle()
	if err != nil {
		return nil, nil
	}
	leaves, err := worker.Expand(ctx, job.ParentURL, job.Codec, job.Language)
	if err != nil {
		// A resolve failure is "that URL is not expandable any more", which is the answer
		// the caller needs. A transport problem is not, and treating it as "gone" would
		// fail a perfectly good track -- both land on the same message, which says the
		// expansion failed rather than claiming the track does not exist.
		return nil, nil
	}
	for index := range leaves {
		candidate := &leaves[index]
		if candidate.AdamID == derefString(job.AdamID) && candidate.Codec == job.Codec {
			s.Leaves.Put(job.ID, *candidate)
			return candidate, nil
		}
	}
	return nil, nil
}

// filesystemDuplicate is the filesystem duplicate check on a real walk.
//
// The rendered file name is produced by the *client* -- `render_song_filename`, which
// knows `dirPathFormat` and the codec's extension -- and it is passed through
// `normalize` exactly once on each side. Feeding the check `leaf.Title` instead would
// double-normalize, and because `normalize` is not idempotent that mis-keys the six
// real library entries whose rendered form begins with a number: the file keys to one
// name and the tag title to another, so the check misses and the file is downloaded
// again, silently, in the safe direction -- which is exactly why nothing would ever
// report it.
func (s *State) filesystemDuplicate(ctx context.Context, leaf *jobs.Leaf) (*dedup.Hit, error) {
	worker, err := s.WorkerHandle()
	if err != nil {
		return nil, err
	}
	rendered, err := worker.RenderFilename(ctx, *leaf, 1)
	if err != nil {
		return nil, err
	}
	scan := library.ScanRoots(s.Settings.LibraryRoots)
	s.warnDegraded(scan)
	return dedup.FindDuplicate(
		scan,
		leaf.AlbumName,
		rendered,
		leaf.ArtistName,
		s.Settings.DedupArtistScope,
	)
}

// mark writes the status and tells every open tab.
//
// The store is re-read afterwards rather than reusing the local row, because `Mark`
// computes `finished_at` and the store is the only place that knows what it set.
// Publishing a row that disagrees with the table is how a queue ends up showing a
// download that finished seconds ago as still running.
func (s *State) mark(job *jobs.Job, status string, fields jobs.MarkFields) {
	if err := s.Store.Mark(job.ID, status, fields); err != nil {
		s.logLine(fmt.Sprintf("could not mark job %d %s: %v", job.ID, status, err))
		return
	}
	s.PublishJob(job.ID)
}

// PublishJob re-reads a row and publishes it, which is what every mutation path ends
// with.
func (s *State) PublishJob(jobID int64) {
	current, err := s.Store.Get(jobID)
	if err != nil {
		return
	}
	s.Broker.Publish(events.JobsChannel, events.Message{
		"kind": "job", "job": JobDict(current),
	})
}

// setCurrentJob tags the progress callback's readings with the job they belong to.
func (s *State) setCurrentJob(jobID *int64) {
	s.mu.Lock()
	s.currentJob = jobID
	s.mu.Unlock()
}

// applyProgress writes one progress reading and publishes it.
//
// **Two ways this can be refused, and both are expected rather than exceptional.** The
// row may be gone -- deleted or cancelled between the chunk and here, with the transfer
// still running upstream and its progress simply no longer wanted -- or the row may
// have *finished*, which is the race that matters: the client cancels its sampler in a
// `finally`, which stops new readings but not one already handed over, so a callback
// can arrive after the job was marked `done`. Without the second check, that callback
// would set the row back to `running`, at which point delete and retry both answer 409
// ("not finished, so there is nothing to retry") and nothing would ever release it: a
// finished job stuck displaying as running. `Store.Mark` refuses the transition, and
// this is the half that turns the refusal into a discarded reading.
func (s *State) applyProgress(jobID int64, reading jobs.Progress) {
	err := s.Store.Mark(jobID, "running", jobs.MarkFields{
		Progress:   reading.Fraction,
		BytesDone:  ptrInt(reading.BytesDone),
		BytesTotal: reading.BytesTotal,
	})
	if err != nil {
		if jobs.AsNotFound(err) || jobs.AsIllegalTransition(err) {
			return
		}
		s.logLine(fmt.Sprintf("could not record progress for job %d: %v", jobID, err))
		return
	}
	s.PublishJob(jobID)
}

// errorName is the Python `TypeName` half of a `TypeName: message` string, which is
// the shape every message in this codebase uses.
func errorName(err error) string {
	var clientError *pyworker.Error
	if errors.As(err, &clientError) {
		return "RipperHostError"
	}
	return fmt.Sprintf("%T", err)
}

func ptr(value string) *string { return &value }

func ptrInt(value int64) *int64 { return &value }

func derefString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
