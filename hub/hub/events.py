"""The in-process broker the API layer streams to the browser over WebSocket.

The hub has one consumer of live state -- `/api/jobs/ws` -- and it is a browser that
was not connected when the interesting thing happened. A download takes minutes; a tab opened
mid-transfer has to render the queue as it is, and the cheapest correct way to do that is to
hand it the recent past before handing it the future.

By default, `subscribe` has two phases, and the order between them is the whole design:

1. **the backlog** -- the last `HISTORY` messages on this channel, oldest first;
2. **the live stream** -- every message published from now on.

**The gap between those phases is the bug this module exists to avoid.** A subscriber that
reads the backlog and *then* registers has a window in which a message is delivered to
nobody. Registering and reading the backlog are therefore one synchronous step with no
`await` between them, and `publish` is synchronous and never blocks on a subscriber -- so on
one event loop no interleaving is possible. `test_a_message_published_after_the_backlog_and_before_the_live_wait_is_not_lost`
pins the window from the outside.

The WebSocket queue does not replay that history: it subscribes with `replay=False` and an
`initial` callback. The broker registers that client first, evaluates the callback without an
`await`, and sends the resulting database snapshot only to that client, before any queued live
updates. This avoids replaying another connection's stale snapshot, broadcasting a new tab's
snapshot to tabs already open, or dropping a change between the snapshot and live stream.

**Nothing is invented for a quiet channel.** Without an explicit `initial` callback, a
subscriber to a channel nothing has been published on waits. A fabricated empty snapshot
would be indistinguishable from a real one and would be a lie that looks like good news. The
queue snapshot is built by the API layer from a real `list()`.

**Encoding.** Messages are compact JSON text, encoded once at publish time. That keeps the
broker independent of the wire protocol and makes serialization errors point to the publisher,
not a connected client. `ensure_ascii=False` preserves Japanese text without expanding it into
`\\uXXXX`; newline and carriage-return characters remain safely escaped inside JSON strings.

**Two limits, both deliberate.** `HISTORY` bounds what a late subscriber is replayed; it does
not bound what a *connected* one can fall behind by either. A browser tab that stops reading
(a background tab, a suspended laptop) used to grow its queue for the life of the process:
measured at 200,000 retained frames, about 8 MB, for one subscriber, with no signal to
anything. So a subscriber that cannot keep up is **told**, by a `SubscriberOverrun` raised
from inside its own stream, and the choice of what to do about it belongs to the layer that
owns the connection -- the WebSocket handler, which closes with a retryable code so the client reconnects and
re-reads a snapshot. The broker deliberately does not make that choice, because it cannot
know whether the stream is a snapshot (where a dropped message costs nothing) or a log line
(where it is the entire point), and it will not quietly hand over the stale frames on the
way out.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

# How many past messages a late subscriber is replayed. Enough for a queue of a few hundred
# jobs to render in full, bounded so that a long-lived process cannot hold an unbounded
# amount of every message it ever published.
HISTORY = 50

# How far a connected subscriber may fall behind before it is given up on. A capacity, not a
# policy: at roughly 200 bytes a frame the worst case is about 200 KB per subscriber, which is
# noise, and a browser that pauses for a few seconds on a stream of a few messages a second
# never comes near it. What happens *at* the limit is `SubscriberOverrun`, and who decides
# what to do about it is the layer that owns the connection.
SUBSCRIBER_QUEUE_SIZE = 1000


class SubscriberOverrun(RuntimeError):
    """A subscriber fell more than its queue allows behind, and was given up on.

    Raised from inside the stream rather than swallowed, so the decision belongs to the layer
    that owns the connection. The obvious answer for the API layer is to let the client
    reconnect and take a fresh snapshot, which is exactly why this is a signal and not a
    silent drop: the
    broker cannot tell whether the frames it is holding are a queue state (where losing one
    costs nothing, because the next snapshot is complete) or a log line (where losing one is
    the whole point of the stream).

    `depth` is how many messages the subscriber missed, which is what tells the API layer
    whether the client was briefly busy or gone entirely.
    """

    def __init__(self, channel: str, depth: int, limit: int) -> None:
        self.channel = channel
        self.depth = depth
        self.limit = limit
        super().__init__(
            f"the subscriber to {channel!r} fell {depth} message(s) behind and was given up"
            f" on: its queue holds at most {limit}. The frames in it are stale, so they are not"
            f" delivered -- re-subscribe and read the current state instead."
        )


def encode_message(data: dict) -> str:
    """Encode one broker payload as compact UTF-8-friendly JSON text."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass(eq=False)
class _Subscriber:
    """One connected reader: its queue, and the count of messages it could not take.

    A small object rather than a bare `asyncio.Queue` because the queue alone cannot carry
    "you have fallen behind": a full queue has no room left for a marker saying that it is
    full, so the count lives beside it and is read on the subscriber's next turn.

    `eq=False` so these are compared and hashed by identity, which is what a `set` of them
    needs: a generated `__eq__` would make two subscribers with the same channel, limit and
    zero overruns compare equal, and the second one to register would then displace the first.
    """

    channel: str
    queue: asyncio.Queue[str]
    limit: int
    overrun: int = 0


@dataclass
class _Channel:
    """One channel's replay buffer and its live subscribers."""

    # A `deque` with a `maxlen` rather than a list trimmed by hand: eviction is the
    # arithmetic, and it cannot be forgotten on a path that adds to it.
    history: deque[str] = field(default_factory=lambda: deque(maxlen=HISTORY))
    subscribers: set[_Subscriber] = field(default_factory=set)


class EventBroker:
    """Fan one published message out to the subscribers of a channel, plus a replay buffer.

    **Not thread-safe, and it does not need to be.** `publish` is called from the scheduler
    task and `subscribe` is awaited by the request handler, both on the one event loop of the
    one process the design assumes; the only cross-thread caller in the codebase is
    `WrapperSupervisor.log_sink`, and the supervisor's own pump already runs on that loop. A
    caller that does have a thread should go through `loop.call_soon_threadsafe`, not
    through this.

    Not a singleton either: it is built behind `creart` like everything else, and two
    brokers in two processes must not be mistaken for one bus.
    """

    def __init__(self, *, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        # A keyword so the API layer can size a log channel differently from a job channel
        # without a second broker; the default is the capacity this use implies.
        self._queue_size = queue_size
        self._channels: dict[str, _Channel] = {}

    def publish(self, channel: str, data: dict) -> None:
        """Send `data` to every current subscriber of `channel`, and to the next ones.

        Synchronous, and it never blocks and never raises because of a slow reader. A bounded
        queue's `put_nowait` can fail, so the full case is an explicit branch: the message is
        not enqueued, the subscriber's `overrun` is counted, and the subscriber is told on its
        next turn. Counting rather than raising keeps one abandoned browser tab from taking the
        scheduler down with it, which is the one thing `publish` must never do.

        Raises `TypeError` for a payload `json` cannot serialise, here rather than inside a
        subscriber -- a `Path` or an unconverted `Job` is a bug in the publisher, and naming
        it there is the only way the traceback points at the code that made it. Raising
        inside a subscriber instead would look like a broken browser connection and would take
        the WebSocket connection down with it.
        """
        message = encode_message(data)
        state = self._state(channel)
        state.history.append(message)
        # Copied because a subscriber that closes while a message is in flight removes
        # itself from the set this loop is walking; the copy makes that a no-op instead of a
        # "set changed size during iteration".
        for subscriber in tuple(state.subscribers):
            if subscriber.queue.full():
                subscriber.overrun += 1
                continue
            subscriber.queue.put_nowait(message)

    async def subscribe(
        self,
        channel: str,
        *,
        replay: bool = True,
        initial: Callable[[], dict] | None = None,
    ) -> AsyncIterator[str]:
        """Yield an optional per-subscriber initial message, backlog, then live messages.

        An async generator, so nothing happens until the caller asks for the first chunk:
        subscription is established on the first read, not on the call. That is deliberate --
        a request handler that built the iterator and was then dropped by the client before
        it iterated would otherwise leak a subscriber for the life of the process, and its
        queue with it. `initial` is evaluated after registration and is only valid when
        `replay=False`; this lets a caller create a fresh snapshot without replaying or
        broadcasting it to other subscribers.

        The two halves are established without an `await` between them, so on one event loop
        there is no window in which a published message could reach neither. See the module
        docstring.

        Raises `SubscriberOverrun` if this subscriber falls further behind than its queue
        allows. It is raised *instead of* the stale frames, not after them: the frames in the
        queue describe a queue state that has already moved on, and delivering them would be
        worse than saying so.
        """
        if initial is not None and replay:
            raise ValueError("an initial message requires replay=False")
        state = self._state(channel)
        backlog = tuple(state.history) if replay else ()
        subscriber = _Subscriber(
            channel=channel, queue=asyncio.Queue(maxsize=self._queue_size), limit=self._queue_size
        )
        state.subscribers.add(subscriber)
        try:
            if initial is not None:
                yield encode_message(initial())
            for message in backlog:
                yield message
            while True:
                # The frame is taken *before* the check, deliberately. A subscriber parked on
                # `get()` is woken by the first message of a burst, and a check placed before
                # the `get` would hand that one stale frame over before noticing -- so this
                # order is what makes "the frames in it are not delivered" true rather than
                # nearly true.
                message = await subscriber.queue.get()
                if subscriber.overrun:
                    raise SubscriberOverrun(channel, subscriber.overrun, subscriber.limit)
                yield message
        finally:
            # Runs on `aclose()`, on a `break` out of `async for`, and on cancellation -- so
            # a browser tab that goes away stops being written to. Without it, every tab that
            # ever connected would be counted on every publish for the life of the process.
            # `discard` is idempotent, so the overrun path above, which unregisters on its way
            # out too, is unaffected.
            state.subscribers.discard(subscriber)

    def subscriber_count(self, channel: str) -> int:
        """How many live subscribers this channel has right now.

        Read-only, and it exists for one reason: the anti-leak `finally` in `subscribe` has
        **no observable consequence from outside the broker**. A subscriber that stays
        registered costs a little memory and one dict-lookup per publish, and nothing anyone
        can look at afterwards says so. So this is the assertion surface for it, the same way
        a raw-connection check is the assertion surface for the schema -- and for the
        same reason: the alternative is a test reading `_channels`, which is worse than a
        method that admits what it is for.

        0 for a channel that has never been used, so a caller does not have to distinguish
        "no subscribers" from "no such channel".
        """
        state = self._channels.get(channel)
        return 0 if state is None else len(state.subscribers)

    def _state(self, channel: str) -> _Channel:
        """This channel's buffer and subscribers, creating them on first use.

        A `setdefault` rather than a subscribe-time read of a possibly-absent key, so that
        publishing to a channel nobody is watching is the same operation as publishing to one
        that is. The entries are bounded by the number of channel names the app uses.
        """
        return self._channels.setdefault(channel, _Channel())
