"""The in-process broker Task 9 streams to the browser over SSE (spec §5, §9).

The hub has one consumer of live state -- `GET /api/jobs/stream` -- and it is a browser that
was not connected when the interesting thing happened. A download takes minutes; a tab opened
mid-transfer has to render the queue as it is, and the cheapest correct way to do that is to
hand it the recent past before handing it the future.

So `subscribe` is two phases, and the order between them is the whole design:

1. **the backlog** -- the last `HISTORY` messages on this channel, oldest first, so a
   late subscriber renders the current queue rather than an empty one;
2. **the live stream** -- every message published from now on.

**The gap between the two phases is the bug this module exists to avoid.** A subscriber that
reads the backlog and *then* registers has a window in which a message is delivered to
nobody: the queue silently loses one update, with no error and no way to notice, and the UI
sits on stale state until some unrelated event wakes it. Registering and reading the backlog
are therefore one synchronous step with no `await` between them, and `publish` is synchronous
and never blocks on a subscriber -- so on one event loop no interleaving is possible at all.
`test_a_message_published_after_the_backlog_and_before_the_live_wait_is_not_lost` pins the
window from the outside.

**Nothing is invented for a quiet channel.** A subscriber to a channel nothing has been
published on waits, and is not handed an empty snapshot: a fabricated `{"kind": "snapshot",
"jobs": []}` would be indistinguishable from a real one, and it would be a lie that looks like
good news. The snapshot is Task 9's to publish, from a real `list()`.

**Frames.** A message is one `data: <json>` line closed by a blank line, per the SSE grammar.
That is only safe because `json.dumps` escapes every character that could end the line --
`\n`, `\r` -- inside a string, and because `ensure_ascii=False` leaves a Japanese album name
as itself rather than as `\\uXXXX`. A payload that broke that assumption would be delivered
as two fields, one of which the client would drop, and the drop would look like a lost
update. `test_a_frame_is_one_sse_data_field_whatever_the_payload_contains` holds the payloads
that would break it.

**Two limits, both deliberate.** `HISTORY` bounds what a late subscriber is replayed; it does
not bound what a *connected* one can fall behind by either. A browser tab that stops reading
(a background tab, a suspended laptop) used to grow its queue for the life of the process:
measured at 200,000 retained frames, about 8 MB, for one subscriber, with no signal to
anything. So a subscriber that cannot keep up is **told**, by a `SubscriberOverrun` raised
from inside its own stream, and the choice of what to do about it belongs to the layer that
owns the connection -- Task 9's SSE handler, which can let the client reconnect and re-read a
snapshot. The broker deliberately does not make that choice, because it cannot know whether
the stream is a snapshot (where a dropped message costs nothing) or a log line (where it is the
entire point), and it will not quietly hand over the stale frames on the way out.

**Nothing is invented for a quiet channel.** A subscriber to a channel nothing has been
published on waits, and is not handed an empty snapshot: a fabricated `{"kind": "snapshot",
"jobs": []}` would be indistinguishable from a real one, and it would be a lie that looks like
good news. The snapshot is Task 9's to publish, from a real `list()`.

**Frames.** A message is one `data: <json>` line closed by a blank line, per the SSE grammar.
That is only safe because `json.dumps` escapes every character that could end the line --
`\n`, `\r` -- inside a string, and because `ensure_ascii=False` leaves a Japanese album name
as itself rather than as `\\uXXXX`. A payload that broke that assumption would be delivered
as two fields, one of which the client would drop, and the drop would look like a lost
update. `test_a_frame_is_one_sse_data_field_whatever_the_payload_contains` holds the payloads
that would break it.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

# How many past messages a late subscriber is replayed. Enough for a queue of a few hundred
# jobs to render in full, bounded so that a long-lived process cannot hold an unbounded
# amount of every message it ever published.
HISTORY = 50

# How far a connected subscriber may fall behind before it is given up on. A capacity, not a
# policy: at roughly 200 bytes a frame the worst case is about 200 KB per subscriber, which is
# noise, and a browser that pauses for a few seconds on a stream of a few messages a second
# never comes near it. What happens *at* the limit is `SubscriberOverrun`, and who decides
# what to do about it is Task 9.
SUBSCRIBER_QUEUE_SIZE = 1000


class SubscriberOverrun(RuntimeError):
    """A subscriber fell more than its queue allows behind, and was given up on.

    Raised from inside the stream rather than swallowed, so the decision belongs to the layer
    that owns the connection. The obvious answer for Task 9 is to let the client reconnect and
    take a fresh snapshot, which is exactly why this is a signal and not a silent drop: the
    broker cannot tell whether the frames it is holding are a queue state (where losing one
    costs nothing, because the next snapshot is complete) or a log line (where losing one is
    the whole point of the stream).

    `depth` is how many messages the subscriber missed, which is what tells Task 9 whether the
    client was briefly busy or gone entirely.
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


def frame(data: dict) -> str:
    """One SSE frame: a single `data:` line, terminated by a blank line.

    Compact separators because this is a stream, and `ensure_ascii=False` because a Japanese
    album name arrives as itself -- the client declares UTF-8, and escaping it would make
    every message three times the bytes for no reader's benefit.
    """
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


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
    one process spec §3 describes; the only cross-thread caller in the codebase is
    `WrapperSupervisor.log_sink`, and Task 5's pump already runs on that loop. A caller that
    does have a thread should go through `loop.call_soon_threadsafe`, not through this.

    Not a singleton either: the brief puts it behind `creart` like everything else, and two
    brokers in two processes must not be mistaken for one bus.
    """

    def __init__(self, *, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        # A keyword so Task 9 can size a log channel differently from a job channel without
        # a second broker; the default is the capacity the brief's own use implies.
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
        the SSE stream down with it.
        """
        message = frame(data)
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

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        """Yield this channel's backlog, then every message published from now on.

        An async generator, so nothing happens until the caller asks for the first chunk:
        subscription is established on the first read, not on the call. That is deliberate --
        a request handler that built the iterator and was then dropped by the client before
        it iterated would otherwise leak a subscriber for the life of the process, and its
        queue with it.

        The two halves are established without an `await` between them, so on one event loop
        there is no window in which a published message could reach neither. See the module
        docstring.

        Raises `SubscriberOverrun` if this subscriber falls further behind than its queue
        allows. It is raised *instead of* the stale frames, not after them: the frames in the
        queue describe a queue state that has already moved on, and delivering them would be
        worse than saying so.
        """
        state = self._state(channel)
        backlog = tuple(state.history)
        subscriber = _Subscriber(
            channel=channel, queue=asyncio.Queue(maxsize=self._queue_size), limit=self._queue_size
        )
        state.subscribers.add(subscriber)
        try:
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
        `spike/task7_schema_check.py` is the assertion surface for the schema -- and for the
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
