// Command amdhub is the hub: one process, one SQLite file, one HTTP port.
//
// It is the port of `python -m hub.app`. The Python entry point existed so that the
// deployment's entry is this repository rather than a `uvicorn` invocation naming a
// factory; the same argument applies here, and it is why the settings, the store, the
// worker and the scheduler are wired in one place and not in a compose file.
//
// **Single process, and it is not a knob.** Everything the hub owns lives on one
// `app.State`: the broker, the job store, the leaf registry, the scheduler, the session
// generation and the wrapper supervisor. Two processes would be two of each -- two
// schedulers racing `ClaimNext` (which is atomic, so no double rip, but two leaf
// registries, so a job could be claimed by a process that never expanded it) and two
// session generations, so a logout on one would not revoke a session minted by the
// other. Scaling out needs an out-of-process broker and a real session table first.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"amdhub/internal/app"
	"amdhub/internal/config"
	"amdhub/internal/wrapper"
)

func main() {
	log.SetFlags(0)
	log.SetPrefix("amdhub: ")
	if err := run(); err != nil {
		log.Printf("%v", err)
		os.Exit(1)
	}
}

func run() error {
	settings, err := config.Load(nil)
	if err != nil {
		return err
	}
	state, err := app.New(settings)
	if err != nil {
		return err
	}

	// A cancel that the signal handler and the drain both use, so a second SIGTERM --
	// which is what `docker stop` escalating looks like -- stops the drain rather than
	// being ignored.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 1. The wrapper. A failure is recorded, not fatal: a fresh install has no Apple
	//    account, and the login page is how that gets fixed.
	if err := state.Supervisor.Start(ctx); err != nil {
		state.SetStartupError(err.Error())
		state.Log.Add("the wrapper did not start: " + err.Error())
		log.Printf("the wrapper did not start: %v", err)
	} else {
		state.SetStartupError("")
	}

	// 2. The client. This one is not fatal either, and for the same reason -- but it is
	//    the difference between a hub that can queue and one that cannot, so it says so
	//    loudly and `/api/jobs` answers 503 with the reason.
	if err := state.StartWorker(ctx); err != nil {
		return err
	}

	// 3. The scheduler, last: it is the only thing that needs both.
	scheduler := make(chan struct{})
	go func() {
		defer close(scheduler)
		state.SchedulerLoop(ctx)
	}()

	router := app.NewRouter(state)
	address := net.JoinHostPort(settings.Bind, fmt.Sprintf("%d", settings.Port))
	server := &http.Server{
		Addr:    address,
		Handler: router,
		// No read timeout: the SSE stream is a response that stays open by design, and
		// a read timeout would cut it every N seconds for no reason. The write timeout
		// is the one that matters for a slow client on the queue page, and it is set
		// well above the largest page.
		ReadHeaderTimeout: 15 * time.Second,
		WriteTimeout:      0,
		IdleTimeout:       120 * time.Second,
	}

	serverError := make(chan error, 1)
	go func() {
		log.Printf("listening on http://%s", address)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverError <- err
		}
	}()

	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)

	select {
	case err := <-serverError:
		state.Stop()
		state.Close(ctx)
		return err
	case <-signals:
	}

	// **The order of shutdown is the whole of it.** The scheduler is stopped first and
	// given `DrainTimeout` for a rip that is in flight, so the row is marked rather than
	// left `running` for a process that is going away; then the client, which releases
	// its own in-flight counter and temporary files; then the wrapper, whose SIGTERM the
	// launcher forwards into its chroot; then the database.
	//
	// `docker stop` sends SIGTERM and waits `stop_grace_period`, which compose sets to
	// 330 s -- 300 s of drain plus the supervisor's own 15 s stop timeout. A second
	// SIGTERM skips the drain, because an operator asking twice means it.
	log.Printf("stopping: draining up to %s for a rip in flight", app.DrainTimeout)
	state.Stop()
	drained := make(chan struct{})
	go func() {
		defer close(drained)
		select {
		case <-scheduler:
		case <-time.After(app.DrainTimeout):
			log.Printf("a job outlived its %s grace; cancelling it", app.DrainTimeout)
			cancel()
			<-scheduler
		}
	}()
	select {
	case <-drained:
	case <-signals:
		log.Printf("a second signal: cancelling the drain")
		cancel()
		<-drained
	}

	cancel()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	_ = server.Shutdown(shutdownCtx)

	closeCtx, closeCancel := context.WithTimeout(context.Background(), app.DrainTimeout)
	defer closeCancel()
	state.Close(closeCtx)
	_ = wrapper.SelfSignal // the constant is documented where it is used: the supervisor
	return nil
}
