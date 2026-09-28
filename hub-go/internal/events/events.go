// Package events is a port of `hub/events.py`: the in-process broker the API
// layer streams to the browser over SSE.
//
// The hub has one consumer of live state -- `GET /api/jobs/stream` -- and it is a
// browser that was not connected when the interesting thing happened. A download
// takes minutes; a tab opened mid-transfer has to render the queue as it is, and
// the cheapest correct way to do that is to hand it the recent past before handing
// it the future.
//
// So `Subscribe` is two phases, and the order between them is the whole design:
//
//  1. **the backlog** -- the last `History` messages on this channel, oldest
//     first, so a late subscriber renders the current queue rather than an empty
//     one;
//  2. **the live stream** -- every message published from now on.
//
// **The gap between the two phases is the bug this package exists to avoid.** A
// subscriber that reads the backlog and *then* registers has a window in which a
// message is delivered to nobody: the queue silently loses one update, with no
// error and no way to notice, and the UI sits on stale state until some unrelated
// event wakes it. Registering and taking the backlog are therefore one
// synchronous step under one mutex, and `Publish` is synchronous and never blocks
// on a subscriber -- so the two cannot interleave at all in Go either.
//
// **Nothing is invented for a quiet channel.** A subscriber to a channel nothing
// has been published on waits, and is not handed an empty snapshot: a fabricated
// `{"kind":"snapshot","jobs":[]}` would be indistinguishable from a real one, and
// it would be a lie that looks like good news. The snapshot is the API layer's to
// publish, from a real `List`.
//
// **Frames.** A message is one `data: <json>` line closed by a blank line, per the
// SSE grammar. That is only safe because the encoder escapes every character that
// could end the line inside a string, and because it does *not* escape a Japanese
// album name into `\uXXXX`: the client declares UTF-8, and escaping would make
// every message three times the bytes for no reader's benefit.
//
// **Two limits, both deliberate.** `History` bounds what a late subscriber is
// replayed. `SubscriberQueueSize` bounds how far a *connected* one can fall
// behind: a browser tab that stops reading (a background tab, a suspended laptop)
// would otherwise grow its queue for the life of the process -- measured in the
// Python original at 200,000 retained frames, about 8 MB, for one subscriber, with
// no signal to anything. So a subscriber that cannot keep up is **told**, by an
// `OverrunError` returned from its own stream, and the choice of what to do about
// it belongs to the layer that owns the connection. The broker deliberately does
// not make that choice, because it cannot know whether the stream is a snapshot
// (where a dropped message costs nothing) or a log line (where it is the entire
// point), and it will not quietly hand over the stale frames on the way out.
package events

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
)

// History is how many past messages a late subscriber is replayed. Enough for a
// queue of a few hundred jobs to render in full, bounded so that a long-lived
// process cannot hold an unbounded amount of every message it ever published.
const History = 50

// SubscriberQueueSize is how far a connected subscriber may fall behind before it
// is given up on. A capacity, not a policy: at roughly 200 bytes a frame the worst
// case is about 200 KB per subscriber, which is noise, and a browser that pauses
// for a few seconds on a stream of a few messages a second never comes near it.
const SubscriberQueueSize = 1000

// Channels the hub publishes on. Named constants rather than literals, because the
// SSE handler subscribes to all of them and a typo would be a channel nobody is
// on: the API would publish successfully and no client would ever see it.
const (
	JobsChannel    = "jobs"
	LibraryChannel = "library"
	WrapperChannel = "wrapper"
	LogChannel     = "log"
)

// AllChannels is every channel the stream carries, in the order the SSE handler
// subscribes to them.
func AllChannels() []string {
	return []string{JobsChannel, LibraryChannel, WrapperChannel, LogChannel}
}

// OverrunError is a subscriber that fell more than its queue allows behind, and
// was given up on.
//
// Returned from inside the stream rather than swallowed, so the decision belongs
// to the layer that owns the connection. The obvious answer for the API layer is
// to let the client reconnect and take a fresh snapshot, which is exactly why this
// is a signal and not a silent drop: the broker cannot tell whether the frames it
// is holding are a queue state (where losing one costs nothing, because the next
// snapshot is complete) or a log line (where losing one is the whole point of the
// stream).
//
// `Depth` is how many messages the subscriber missed, which is what tells the API
// layer whether the client was briefly busy or gone entirely.
type OverrunError struct {
	Channel string
	Depth   int
	Limit   int
}

func (e *OverrunError) Error() string {
	return fmt.Sprintf("the subscriber to %q fell %d message(s) behind and was given up on: "+
		"its queue holds at most %d. The frames in it are stale, so they are not delivered -- "+
		"re-subscribe and read the current state instead.", e.Channel, e.Depth, e.Limit)
}

// Message is one published payload. The broker does not interpret it: a `kind`
// field is the API layer's convention, and the broker's job is to deliver it.
type Message = map[string]any

// Frame is one SSE frame: a single `data:` line, terminated by a blank line.
//
// Compact separators because this is a stream. An error from the encoder is
// impossible for the payloads the hub publishes (maps, slices, strings, numbers,
// booleans), so it is handled by encoding a message that says so rather than by
// panicking inside a stream handler -- a stream that dies mid-frame would leave
// the client reconnecting for ever.
func Frame(data Message) string {
	encoded, err := json.Marshal(data)
	if err != nil {
		encoded = []byte(`{"kind":"log","line":"the hub could not encode an event"}`)
	}
	return "data: " + string(encoded) + "\n\n"
}

// subscriber is one connected reader: its queue and the count of messages it could
// not take.
//
// A small object rather than a bare channel because the channel alone cannot carry
// "you have fallen behind": a full channel has no room left for a marker saying
// that it is full, so the count lives beside it and is read on the subscriber's
// next turn.
type subscriber struct {
	queue    chan Message
	overruns int
	closed   bool
}

type channel struct {
	history []Message
	subs    map[*subscriber]struct{}
}

// Broker is the fan-out. One per process, held on the app state.
//
// Every method is safe for concurrent use: the scheduler publishes from worker
// goroutines, requests publish from handler goroutines, and each SSE connection
// reads from its own.
type Broker struct {
	mu       sync.Mutex
	channels map[string]*channel
}

// New returns an empty broker with a channel entry for every name the hub
// publishes on, so that a subscriber to a quiet channel is a subscriber with no
// messages rather than a subscriber to a channel that does not exist.
func New() *Broker {
	return &Broker{channels: map[string]*channel{}}
}

func (b *Broker) channel(name string) *channel {
	ch, ok := b.channels[name]
	if !ok {
		ch = &channel{subs: map[*subscriber]struct{}{}}
		b.channels[name] = ch
	}
	return ch
}

// Publish delivers one message to every subscriber of one channel, and never
// blocks.
//
// A subscriber whose queue is full does not stall the publisher -- which would let
// one backgrounded browser tab stop the scheduler -- and is not silently dropped
// either: its overrun count is incremented, and its next read returns an
// `OverrunError` instead of stale frames.
func (b *Broker) Publish(channelName string, data Message) {
	b.mu.Lock()
	defer b.mu.Unlock()
	ch := b.channel(channelName)
	ch.history = append(ch.history, data)
	if len(ch.history) > History {
		ch.history = ch.history[len(ch.history)-History:]
	}
	for sub := range ch.subs {
		select {
		case sub.queue <- data:
		default:
			sub.overruns++
		}
	}
}

// Subscription is one reader's view of a channel, and it must be closed.
type Subscription struct {
	broker  *Broker
	channel string
	sub     *subscriber
	backlog []Message
}

// Subscribe registers a reader and takes the backlog in one step.
//
// The backlog comes first because it is the past, and it is delivered from this
// struct rather than through the queue so that a subscriber that registers and
// then reads can never see a message published *before* it registered arrive after
// one published after it.
func (b *Broker) Subscribe(channelNames []string) []*Subscription {
	b.mu.Lock()
	defer b.mu.Unlock()
	subs := make([]*Subscription, 0, len(channelNames))
	for _, name := range channelNames {
		ch := b.channel(name)
		sub := &subscriber{queue: make(chan Message, SubscriberQueueSize)}
		ch.subs[sub] = struct{}{}
		// Copied, because the channel's own slice keeps being truncated as new
		// messages arrive, and a subscriber holding the same backing array would
		// watch its own backlog change underneath it.
		backlog := append([]Message(nil), ch.history...)
		subs = append(subs, &Subscription{broker: b, channel: name, sub: sub, backlog: backlog})
	}
	return subs
}

// Backlog is the messages published before this subscription existed, oldest
// first.
func (s *Subscription) Backlog() []Message { return s.backlog }

// Channel is the channel this subscription is on.
func (s *Subscription) Channel() string { return s.channel }

// Next blocks until a message arrives, and reports false when the subscription is
// closed. An `OverrunError` is returned as the error, with no message.
//
// The overrun check happens *before* the receive, so a subscriber that fell behind
// is told at the first opportunity rather than after draining a thousand stale
// frames.
func (s *Subscription) Next() (Message, error, bool) {
	for {
		s.broker.mu.Lock()
		pending := s.sub.overruns
		s.sub.overruns = 0
		closed := s.sub.closed
		s.broker.mu.Unlock()
		if closed {
			return nil, nil, false
		}
		if pending > 0 {
			return nil, &OverrunError{
				Channel: s.channel,
				Depth:   pending,
				Limit:   SubscriberQueueSize,
			}, true
		}
		message, ok := <-s.sub.queue
		if !ok {
			return nil, nil, false
		}
		return message, nil, true
	}
}

// NextContext is `Next`, but it also gives up when the caller's context is done.
//
// **This is what keeps a closed tab from holding a subscriber for the life of the
// process.** The request context is cancelled when the client goes away, and the SSE
// handler blocks here between frames -- without this it would block forever on a channel
// nobody happens to publish to, and the hub would accumulate one dead subscriber per tab
// ever opened. That is the leak `finally: aclose()` prevents on the Python side, and the
// reason the handler can rely on `defer subscription.Close()` being reached.
//
// The overrun check is `Next`'s, and it stays before the receive for the same reason.
func (s *Subscription) NextContext(ctx context.Context) (Message, error, bool) {
	for {
		s.broker.mu.Lock()
		pending := s.sub.overruns
		s.sub.overruns = 0
		closed := s.sub.closed
		s.broker.mu.Unlock()
		if closed {
			return nil, nil, false
		}
		if pending > 0 {
			return nil, &OverrunError{
				Channel: s.channel,
				Depth:   pending,
				Limit:   SubscriberQueueSize,
			}, true
		}
		select {
		case message, ok := <-s.sub.queue:
			if !ok {
				return nil, nil, false
			}
			return message, nil, true
		case <-ctx.Done():
			return nil, ctx.Err(), false
		}
	}
}

// Close removes the subscriber. Idempotent.
func (s *Subscription) Close() {
	s.broker.mu.Lock()
	defer s.broker.mu.Unlock()
	if s.sub.closed {
		return
	}
	s.sub.closed = true
	if ch, ok := s.broker.channels[s.channel]; ok {
		delete(ch.subs, s.sub)
	}
	close(s.sub.queue)
}

// SubscriberCount is how many readers a channel has, which is what a test needs to
// assert that a disconnected client was cleaned up.
func (b *Broker) SubscriberCount(channelName string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	if ch, ok := b.channels[channelName]; ok {
		return len(ch.subs)
	}
	return 0
}

// ErrClosed is returned by a helper that wants to tell "the stream ended" from
// "the stream broke".
var ErrClosed = errors.New("the subscription is closed")
