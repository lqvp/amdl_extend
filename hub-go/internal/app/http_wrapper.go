package app

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"amdhub/internal/wrapper"
)

// TwoFADeadline is the payload's own poll loop, `20 x 3s`. Not the supervisor's
// configurable TTL -- this is what the hub tells the user it will wait, and it must not
// promise more than the child honours.
const TwoFADeadline = 60 * time.Second

// The supervisor's two readiness messages, quoted.
//
// `classifySupervisorFailure` is a fallback, not the primary path: wherever the state
// can be *observed* it is, and these are only read for the state that cannot be
// re-probed.
const (
	noAccountMarker = "no account is logged in on the wrapper"
	notReadyMarker  = "did not become ready"
)

// What the hub says when it can observe the state itself: the wrapper is up, `/status`
// answers, and `regions` is empty. Written so that the two states cannot be confused by
// a reader -- it says "nothing needs to be waited for", because that is the part a user
// acts on.
const noAccountDetail = "no account is logged in on the wrapper: it is up and answering " +
	"/status, but regions is empty, so it cannot serve a download. Log in from this page; " +
	"the wrapper itself is ready, so there is nothing to wait for."

const notRunningDetail = "no wrapper is running. Start it from this page; if it was " +
	"started and stopped, the reason is in the log below."

// classifySupervisorFailure is "no-account" or "unavailable", from the supervisor's own
// wording.
//
// **A live probe is better and is used wherever it can be.** `WrapperState` asks the
// wrapper what is actually true, and that is the answer this only approximates. The
// case it exists for is a `start()` that *failed*: the supervisor tears the child down
// before it returns the error, so by the time the hub sees it there is nothing left to
// ask, and the message is the only evidence in existence.
//
// Matching on the collaborator's prose is a real dependence and worth naming: if
// upstream rewrites either sentence this returns "unavailable" for a no-account state
// -- which shows the *correct* message (the verbatim one is always what reaches the
// user) with a coarser key beside it.
func classifySupervisorFailure(message string) string {
	if strings.Contains(message, noAccountMarker) {
		return "no-account"
	}
	return "unavailable"
}

// WrapperState is what the wrapper is doing, as the UI needs it.
//
// Every field is a fact about the wrapper rather than about this hub's opinion of it:
// `Running` is the supervisor's own, `Regions` is the payload's, and `Detail` is either
// a message this file wrote (for the state it observed) or the supervisor's, unchanged.
func (s *State) WrapperState(ctx context.Context) wrapperView {
	supervisor := s.Supervisor
	view := wrapperView{
		Running: supervisor.Running(),
		Adopted: supervisor.Adopted(),
		Pid:     supervisor.Pid(),
		Port:    supervisor.BoundPort(),
		Regions: []string{},
	}

	if view.Running {
		payload, err := supervisor.Status(ctx)
		if err != nil {
			view.Problem = "unavailable"
			view.Detail = fmt.Sprintf("%T: %v", err, err)
		} else {
			view.Regions = regionList(payload)
			if len(view.Regions) == 0 {
				// The distinct state: serving, healthy, and unable to serve a download
				// because no account is on it.
				view.Problem = "no-account"
				view.Detail = noAccountDetail
			}
		}
	} else {
		// A `start()` that failed is the one case where nothing can be probed, and the
		// message it left behind is the only evidence there is. It is classified rather
		// than collapsed, because "not running because nobody has logged in" and "not
		// running because it did not start" need different buttons -- and the first is the
		// state every fresh install is in.
		view.Detail = s.StartupError()
		if view.Detail == "" {
			view.Detail = notRunningDetail
		} else {
			view.Problem = classifySupervisorFailure(view.Detail)
		}
		if view.Problem == "" {
			view.Problem = "unavailable"
		}
	}
	view.Ready = view.Problem == ""
	return view
}

func regionList(payload map[string]any) []string {
	regions, _ := payload["regions"].([]any)
	out := make([]string, 0, len(regions))
	for _, region := range regions {
		out = append(out, fmt.Sprintf("%v", region))
	}
	return out
}

// handleWrapperStart starts the wrapper, or reports the supervisor's own reason for not
// being able to.
//
// `startup_error` is kept so that a later `/api/status` can still explain a wrapper
// that is not running: without it, a failed start becomes "not running" with no reason
// the moment the request returns, which is the state the user has to debug from.
func (s *State) handleWrapperStart(w http.ResponseWriter, r *http.Request) {
	status, body := s.startWrapper(r.Context())
	writeJSON(w, status, body)
}

func (s *State) startWrapper(ctx context.Context) (int, map[string]any) {
	if err := s.Supervisor.Start(ctx); err != nil {
		s.SetStartupError(err.Error())
		return http.StatusBadGateway, map[string]any{
			"detail":  err.Error(),
			"problem": classifySupervisorFailure(err.Error()),
		}
	}
	s.SetStartupError("")
	return http.StatusOK, s.WrapperState(ctx).asMap()
}

func (v wrapperView) asMap() map[string]any {
	return map[string]any{
		"running": v.Running, "adopted": v.Adopted, "pid": v.Pid, "port": v.Port,
		"regions": v.Regions, "ready": v.Ready, "problem": emptyToNil(v.Problem),
		"detail": emptyToNil(v.Detail),
	}
}

func emptyToNil(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// handleWrapperStop stops what this hub started, and nothing else.
func (s *State) handleWrapperStop(w http.ResponseWriter, r *http.Request) {
	s.Supervisor.Stop()
	s.SetStartupError("")
	writeJSON(w, http.StatusOK, s.WrapperState(r.Context()).asMap())
}

// handleWrapperRestart is a stop and a start, so that "restart" cannot mean two
// different things in two places.
func (s *State) handleWrapperRestart(w http.ResponseWriter, r *http.Request) {
	s.Supervisor.Stop()
	status, body := s.startWrapper(r.Context())
	writeJSON(w, status, body)
}

// loginBody is `POST /api/wrapper/login`'s body. Every field optional, so a missing one
// is a wrong password rather than a 422: a 422 would say "field required" where a 401
// says "that did not work", and the second is both true and the only answer this route
// is allowed to give.
type loginBody struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// handleWrapperLogin starts an Apple login, and answers with either a challenge or a
// finished restart.
//
// Two shapes of success, because upstream has two. The supervisor runs the launcher's
// login mode and returns once the child *asks for a 2FA code*; an account that needs
// none never gets that far, and the supervisor reports it as an error whose own text
// says "if this account needs no 2FA the login is done". So a returned error here is
// not necessarily a failure, and the only honest way to tell is to restart the wrapper
// and look at `regions` -- which is what `finishLogin` does, for both paths.
func (s *State) handleWrapperLogin(w http.ResponseWriter, r *http.Request) {
	var body loginBody
	decodeBody(r, &body)
	challenge, err := s.Supervisor.Login(r.Context(), body.Username, body.Password)
	if err != nil {
		status, response := s.finishLogin(r.Context(), nil, err)
		writeJSON(w, status, response)
		return
	}
	s.setPending2FA(challenge)
	writeJSON(w, http.StatusOK, map[string]any{
		"challenge_id": challenge.ID,
		"expires_at":   float64(challenge.ExpiresAt.UnixMilli()) / 1000,
		"expires_in":   max(0, time.Until(challenge.ExpiresAt).Seconds()),
	})
}

// twoFABody is `POST /api/wrapper/login/2fa`'s body.
//
// The challenge id comes from this process's own memory of the outstanding login rather
// than from the request: the supervisor holds at most one login child, so there is
// exactly one challenge that can be live, and accepting a caller-supplied id would be
// accepting a second way of naming it for no gain.
type twoFABody struct {
	ChallengeID string `json:"challenge_id"`
	Code        string `json:"code"`
}

// handleWrapperTwoFA hands the code to the waiting child, then restarts and only then
// reports.
func (s *State) handleWrapperTwoFA(w http.ResponseWriter, r *http.Request) {
	var body twoFABody
	decodeBody(r, &body)
	challenge := s.pending2FA()
	if challenge == nil {
		fail(w, http.StatusConflict, "there is no 2FA login in progress, so there is "+
			"nothing to hand a code to. Log in again.")
		return
	}
	if err := s.Supervisor.Submit2FA(challenge.ID, body.Code); err != nil {
		status, response := s.finishLogin(r.Context(), nil, err)
		writeJSON(w, status, response)
		return
	}
	s.setPending2FA(nil)
	status, response := s.finishLogin(r.Context(), challenge, nil)
	writeJSON(w, status, response)
}

// finishLogin restarts the wrapper and reports its state, which is the only honest way
// to answer a login.
//
// **A successful login is followed by a restart.** The payload reads its token cache at
// process start, so a wrapper that was already serving when the account was logged into
// keeps serving with the old -- or no -- account. So the restart happens before the
// answer, and only a restarted wrapper that actually reports regions is a success.
func (s *State) finishLogin(ctx context.Context, challenge *wrapper.Challenge, loginErr error) (int, map[string]any) {
	s.setPending2FA(nil)
	s.Supervisor.Stop()
	status, body := s.startWrapper(ctx)
	resumed := int64(0)
	if status == http.StatusOK {
		if count, err := s.Store.ResumeWaiting(); err == nil {
			resumed = count
		}
	}
	problem := body["problem"]
	if problem != nil {
		body["login_error"] = errorString(loginErr)
		return http.StatusBadGateway, body
	}
	return http.StatusOK, map[string]any{
		"ok":      true,
		"wrapper": body,
		"resumed": resumed,
	}
}

func errorString(err error) any {
	if err == nil {
		return nil
	}
	return err.Error()
}

// pending2FA is the outstanding challenge, held on the state so the two routes agree
// about which login is in progress.
func (s *State) pending2FA() *wrapper.Challenge {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.pendingChallenge
}

func (s *State) setPending2FA(challenge *wrapper.Challenge) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pendingChallenge = challenge
}
