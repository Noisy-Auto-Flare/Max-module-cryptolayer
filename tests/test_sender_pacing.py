"""Task 6: paced Sender — jitter, error classification, bounded queue.

Fully deterministic: ``random.uniform`` is monkeypatched to a constant,
the session's stop_event is a RECORDING fake (``wait()`` calls are
logged, never really slept through), and vkmax's send_message is replaced
by a programmable async fake behind the ``_get_send_message`` seam.
No real sleep longer than ~0.3 s exists in this file; a grep-based guard
proves ``time.sleep`` is absent from main.py waiting paths.
"""

import asyncio
import importlib
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, ".")

MaxMain = importlib.import_module("Max.main")

PAUSE = 3.0  # constant "jitter" value injected in place of random.uniform


class RecordingEvent:
    """Drop-in threading.Event stand-in that RECORDS wait() timeouts.

    ``wait(timeout)`` never sleeps: it records the requested timeout and
    returns immediately (False unless already set) — so pacing/backoff
    delays cost nothing while still being observable for assertions.
    """

    def __init__(self):
        self._flag = threading.Event()
        self.calls = []

    def wait(self, timeout=None):
        self.calls.append(timeout)
        return self._flag.wait(0)

    def set(self):
        self._flag.set()

    def clear(self):
        self._flag.clear()

    def is_set(self):
        return self._flag.is_set()


class FakeSendClient:
    """Placeholder client — the fake send function ignores it."""

    def __init__(self):
        self.disconnect_calls = 0

    async def disconnect(self):
        self.disconnect_calls += 1


def make_fake_send(script):
    """Build a programmable async ``send_message`` replacement.

    ``script``: list consumed per call; an Exception instance is raised,
    anything else is returned as the server answer. Returns
    ``(fake_fn, calls)`` where calls records ``(chat_id, text)`` tuples.
    """
    calls = []
    lock = threading.Lock()

    async def fake_send_message(
        client, chat_id, text, notify=True, reply_to=None, attaches=None
    ):
        with lock:
            calls.append((chat_id, text))
            action = script.pop(0) if script else "ok"
        if isinstance(action, BaseException):
            raise action
        return action

    return fake_send_message, calls


class AsyncLoop:
    """A real running asyncio loop on a daemon thread (session['loop'])."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def make_sender(
    monkeypatch,
    script=None,
    min_pause=2.0,
    max_pause=4.0,
    stop_event=None,
    queue_maxsize=None,
    pause_value=PAUSE,
):
    """Wire a Sender against fakes; returns (sender, event, send_calls)."""
    loop_box = AsyncLoop()
    fake_send, calls = make_fake_send([] if script is None else script)
    monkeypatch.setattr(MaxMain, "_get_send_message", lambda: fake_send)
    monkeypatch.setattr(MaxMain.random, "uniform", lambda a, b: pause_value)
    event = RecordingEvent() if stop_event is None else stop_event
    session = {
        "client": FakeSendClient(),
        "loop": loop_box.loop,
        "chat_id": 42,
        "my_id": 777,
        "stop_event": event,
        "min_pause": min_pause,
        "max_pause": max_pause,
    }
    sender = MaxMain.Max.Sender(["t", "d", "42", "", ""], "42", session)
    if queue_maxsize is not None:
        # Shrink AFTER construction by swapping the queue object itself
        # (mirrors _QUEUE_MAXSIZE semantics without re-deriving __init__).
        import queue as queue_mod

        sender._queue = queue_mod.Queue(maxsize=queue_maxsize)
    return sender, event, calls, loop_box


def drain(sender, calls, expected, timeout=5.0):
    """Wait until the fake send recorded ``expected`` calls."""
    deadline = time.monotonic() + timeout
    while len(calls) < expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == expected, calls


class TestHappyPath:
    def test_five_messages_sent_sequentially_with_pauses_at_least_min(
        self, monkeypatch
    ):
        sender, event, calls, loop_box = make_sender(
            monkeypatch, min_pause=2.0, max_pause=4.0
        )
        try:
            for i in range(5):
                sender.send(f"msg-{i}")
            drain(sender, calls, 5)

            assert [text for _, text in calls] == [
                f"msg-{i}" for i in range(5)
            ]
            assert all(chat_id == 42 for chat_id, _ in calls)
            # Exactly one pacing pause per message, each >= min_pause.
            assert event.calls == [PAUSE] * 5
            assert all(pause >= 2.0 for pause in event.calls)
            assert not sender.session["stop_event"].is_set()
        finally:
            loop_box.stop()


class TestTransientRetries:
    def test_two_transient_errors_two_growing_backoffs_then_success(
        self, monkeypatch
    ):
        script = [
            Exception("connection timed out"),
            MaxMain._TransientSendError("server silence"),  # None-result path
            "sent",
        ]
        sender, event, calls, loop_box = make_sender(monkeypatch, script)
        try:
            sender.send("retry-me")
            # 3 ATTEMPTS: 2 failures + final success (fake records every
            # attempt, delivered or not; RecordingEvent makes it instant).
            drain(sender, calls, 3, timeout=10)

            # Pacing pause, then backoff base 5 s, then doubled 10 s.
            assert event.calls == [PAUSE, MaxMain._BACKOFF_BASE_S, 10.0]
            assert event.calls[2] > event.calls[1]
            assert calls == [(42, "retry-me")] * 3
        finally:
            loop_box.stop()

    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (Exception("flood limit exceeded"), "transient"),
            (
                asyncio.TimeoutError(),
                "transient",
            ),  # wait_for expiry — transient BY TYPE
        ],
        ids=["flood-marker", "timeout-type"],
    )
    def test_flood_and_timeout_markers_are_transient(self, monkeypatch, exc, kind):
        assert MaxMain._classify_send_error(exc) == kind

    def test_success_resets_failure_counter_next_message_retries_full(
        self, monkeypatch
    ):
        script = [
            Exception("temporary network hiccup"),
            "sent",
            "sent",
        ]
        sender, event, calls, loop_box = make_sender(monkeypatch, script)
        try:
            sender.send("first")
            sender.send("second")
            # 3 attempts: first fails once + retry, then two deliveries.
            drain(sender, calls, 3, timeout=10)

            # First message: pacing + one backoff; second: fresh pacing,
            # NO leftover backoff (counter reset by the success).
            assert event.calls.count(MaxMain._BACKOFF_BASE_S) == 1
            assert calls == [(42, "first"), (42, "first"), (42, "second")]
        finally:
            loop_box.stop()


class TestTerminalFailure:
    def test_terminal_error_stops_worker_without_stop_event(self, monkeypatch):
        script = [Exception("AUTH_FAILED: invalid token")]
        sender, event, calls, loop_box = make_sender(monkeypatch, script)
        try:
            sender.send("doomed")
            sender.join_worker(timeout=5)

            assert sender.worker_stopped
            assert not event.is_set(), "worker stopped WITHOUT stop_event"
            # The fake records the ATTEMPT (it raises before answering):
            # exactly one attempt, nothing ever delivered/queued.
            assert calls == [(42, "doomed")]
            assert sender._queue.empty()  # terminal path cleared the queue
        finally:
            loop_box.stop()

    def test_unknown_errors_become_terminal_after_five_consecutive_failures(
        self, monkeypatch
    ):
        script = [
            Exception(f"weird failure #{i}") for i in range(5)
        ] + ["never reached"]
        sender, event, calls, loop_box = make_sender(
            monkeypatch, script
        )
        try:
            sender.send("mystery")
            sender.join_worker(timeout=5)

            assert sender.worker_stopped
            assert not event.is_set()
            # 4 backoffs before the 5th attempt trips the limit.
            backoffs = [
                c
                for c in event.calls
                if c != PAUSE
            ]
            assert backoffs[:4] == [5.0, 10.0, 20.0, 40.0]
        finally:
            loop_box.stop()

    def test_permission_marker_is_terminal_immediately(self, monkeypatch):
        script = [Exception("permission denied for dialog")]
        sender, event, calls, loop_box = make_sender(monkeypatch, script)
        try:
            sender.send("nope")
            sender.join_worker(timeout=5)

            assert sender.worker_stopped
            assert not event.is_set()
            assert calls == [(42, "nope")]  # one attempt, never delivered
        finally:
            loop_box.stop()


class TestQueueOverflow:
    def test_overflow_drops_with_warning_and_send_returns_fast(
        self, monkeypatch, caplog
    ):
        sender, _event, calls, loop_box = make_sender(
            monkeypatch, queue_maxsize=2
        )
        try:
            # Pin the worker off so the queue stays deterministically full:
            # the unit under test here is send()'s put_nowait Full branch.
            monkeypatch.setattr(sender, "_ensure_worker", lambda: None)
            sender._queue.put("a")
            sender._queue.put("b")
            assert sender._queue.full()

            with caplog.at_level(logging.WARNING, logger="Max.main"):
                start = time.perf_counter()
                sender.send("overflowing")
                elapsed_ms = (time.perf_counter() - start) * 1000

            assert elapsed_ms < 50, f"send() blocked {elapsed_ms:.1f} ms"
            assert any(
                "dropping message" in rec.getMessage() for rec in caplog.records
            )
            assert sender._queue.qsize() == 2  # dropped, queue untouched
            assert calls == []
        finally:
            loop_box.stop()


class TestInterruptibleStop:
    def test_stop_event_during_pacing_wait_exits_under_2s(self, monkeypatch):
        # REAL threading.Event here: we exercise genuine interruptibility.
        real_event = threading.Event()
        sender, _recording, calls, loop_box = make_sender(
            monkeypatch,
            stop_event=real_event,
            pause_value=30.0,  # would block 30 s if wait were not interruptible
        )
        try:
            sender.send("will-be-dropped")
            time.sleep(0.3)  # let the worker enter the pacing wait
            real_event.set()

            start = time.perf_counter()
            sender.join_worker(timeout=2)
            elapsed = time.perf_counter() - start

            assert not sender._worker_thread.is_alive()
            assert elapsed < 2.0
            assert calls == []  # dropped mid-pause, never sent
        finally:
            loop_box.stop()

    def test_stop_event_during_backoff_wait_exits_without_sending(
        self, monkeypatch
    ):
        real_event = threading.Event()
        script = [Exception("timed out")] * 10
        sender, _recording, calls, loop_box = make_sender(
            monkeypatch, script, stop_event=real_event
        )
        try:
            sender.send("stuck")
            time.sleep(0.3)  # first attempt fails, worker enters backoff
            real_event.set()

            sender.join_worker(timeout=2)
            assert not sender._worker_thread.is_alive()
            assert len(calls) <= 1  # at most the single failed attempt
        finally:
            loop_box.stop()


class TestStaticGuards:
    def test_no_time_sleep_in_main_waiting_paths(self):
        # Guard against call syntax anywhere in main.py (docstring mentions
        # of the bare name without a paren are fine). Only main.py is
        # checked — this file's sub-10ms polling sleeps are test harness.
        source = Path("Max/main.py").read_text(encoding="utf-8")
        assert "time.sleep(" not in source

    def test_no_typing_emulation_in_module(self):
        source = Path("Max/main.py").read_text(encoding="utf-8")
        assert "set_status" not in source
        assert "typing_status" not in source
