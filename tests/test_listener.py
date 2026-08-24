"""Task 5: Listener — op=128 parsing, dialog/sender filtering, reconnect.

Fully deterministic: the session's stop_event is a RECORDING fake
(``wait()`` calls are logged, never really slept through — so the whole
5→10→20→40→60 s reconnect schedule costs zero wall time), vkmax's client
is replaced by a programmable FakeClient (scriptable connect/login
failures + manual packet dispatch), and ``_build_client`` is monkeypatched
at its documented seam. No real sleep longer than ~1 s exists in this
file (join/drain timeouts only).
"""

import asyncio
import importlib
import inspect
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")

MaxMain = importlib.import_module("Max.main")

CHAT_ID = 42
MY_ID = 777
FOREIGN_SENDER = 999


class RecordingEvent:
    """Drop-in threading.Event stand-in that RECORDS wait() timeouts.

    Same fake as test_sender_pacing: ``wait(timeout)`` never sleeps, it
    records the requested timeout and returns immediately (False unless
    already set) — reconnect backoff pauses become observable and free.
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


class DeadTask:
    """Duck-typed stand-in for a FINISHED recv task (_ws_dead → True)."""

    def done(self):
        return True


class FakeClient:
    """Programmable vkmax MaxClient replacement.

    ``fail_connect`` / ``fail_login``: exception instances raised by the
    respective calls (None = success). Packets are delivered manually via
    :meth:`dispatch` exactly like vkmax does it: an asyncio task running
    the registered async callback with ``(client, packet)``.
    """

    _recv_task: object  # Future while healthy; DeadTask simulates a drop

    def __init__(self, fail_connect=None, fail_login=None):
        self.fail_connect = fail_connect
        self.fail_login = fail_login
        self.callbacks = []
        self.connect_calls = 0
        self.login_calls = 0
        self.invokes = []  # (opcode, payload) recorder for raw invokes
        # Pending future ⇒ connection healthy (mirrors _recv_task);
        # tests swap in a duck-typed DeadTask to simulate a drop.
        self._recv_task = None

    # contract §3: SYNC registration, async fn with two positional args
    def set_packet_callback(self, function):
        self.callbacks.append(function)

    async def invoke_method(self, opcode=0, payload=None, retries=2):
        self.invokes.append((opcode, payload))
        return None

    async def connect(self):
        if self.fail_connect is not None:
            raise self.fail_connect
        self.connect_calls += 1
        loop = asyncio.get_running_loop()
        self._recv_task = loop.create_future()

    async def login_by_token(self, token, device_id=None):
        if self.fail_login is not None:
            raise self.fail_login
        self.login_calls += 1
        return {"payload": {"profile": {"id": MY_ID}}}

    async def dispatch(self, packet):
        cb = self.callbacks[-1]
        await cb(self, packet)


def make_listener(monkeypatch, clients=None):
    """Wire a Listener against fakes; returns a handle namespace.

    ``clients``: list of clients handed out by _build_client one per
    reconnect attempt (last one repeats when exhausted).
    """
    loop_box = AsyncLoop()
    built = []
    scripted = list(clients or [])
    lock = threading.Lock()

    def fake_build_client(token, device_id):
        with lock:
            client = (
                scripted.pop(0)
                if scripted
                else (built[-1] if built else FakeClient())
            )
            built.append(client)
        return client

    monkeypatch.setattr(MaxMain, "_build_client", fake_build_client)

    event = RecordingEvent()
    session = {
        "client": FakeClient(),
        "loop": loop_box.loop,
        "chat_id": CHAT_ID,
        "my_id": MY_ID,
        "stop_event": event,
        "min_pause": 2.0,
        "max_pause": 6.0,
    }
    ingested = []
    ingested_event = threading.Event()

    def ingester(text):
        ingested.append(text)
        ingested_event.set()

    listener = MaxMain.Max.Listener(
        ["tok", "dev", str(CHAT_ID), "", ""], ingester, str(CHAT_ID), session
    )
    return SimpleNamespace(
        listener=listener,
        session=session,
        event=event,
        ingested=ingested,
        ingested_event=ingested_event,
        built=built,
        loop_box=loop_box,
    )


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


def dispatch_sync(handle, client, packet, timeout=2.0):
    """Deliver one packet through the registered callback on the loop."""
    cb = client.callbacks[-1]
    future = asyncio.run_coroutine_threadsafe(cb(client, packet), handle.loop_box.loop)
    future.result(timeout)


def start_listen(handle):
    thread = threading.Thread(target=handle.listener.listen, daemon=True)
    thread.start()
    return thread


def wait_until(predicate, timeout=5.0, message="condition not met"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(message)
        time.sleep(0.01)


def msg_packet(chat_id=CHAT_ID, sender=FOREIGN_SENDER, text="hello"):
    return {
        "ver": 11,
        "cmd": 0,
        "seq": 1,
        "opcode": 128,
        "payload": {
            "chatId": chat_id,
            "message": {"sender": sender, "text": text, "id": "m1"},
        },
    }


class TestHappyPath:
    def test_foreign_sender_our_chat_delivers_exactly_once(self, monkeypatch):
        handle = make_listener(monkeypatch)
        first_client = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(
                lambda: bool(first_client.callbacks),
                message="callback was never registered",
            )
            # Contract §3 shape: async fn taking two positional args.
            cb = first_client.callbacks[-1]
            assert inspect.iscoroutinefunction(cb)
            assert len(inspect.signature(cb).parameters) == 2

            dispatch_sync(handle, first_client, msg_packet())
            wait_until(
                lambda: len(handle.ingested) >= 1,
                message="ingester was never called",
            )

            assert handle.ingested == ["hello"]  # EXACTLY once, with text

            handle.event.set()
            thread.join(timeout=2)
            assert not thread.is_alive()
        finally:
            handle.loop_box.stop()

    def test_registration_is_sync_call_before_receiving(self, monkeypatch):
        # listen() must register synchronously (no await / no task) —
        # proven indirectly: registration happens even when stop_event is
        # ALREADY set (listen returns immediately but still registers).
        handle = make_listener(monkeypatch)
        handle.event.set()
        handle.listener.listen()  # blocking call returns right away
        assert len(handle.session["client"].callbacks) == 1

    def test_read_receipt_fired_after_humanized_delay(self, monkeypatch):
        """Contract §15: a delivered message is marked read (opcode 50,
        READ_MESSAGE) ~1 s later against session['client']."""
        monkeypatch.setattr(MaxMain, "_READ_RECEIPT_DELAY_S", 0.01)
        handle = make_listener(monkeypatch)
        client = handle.session["client"]
        handle.listener._register_on(client)
        try:
            dispatch_sync(handle, client, msg_packet())  # id="m1"
            wait_until(
                lambda: client.invokes,
                message="read receipt was never invoked",
            )
            assert client.invokes[0][0] == 50
            payload = client.invokes[0][1]
            assert payload["type"] == "READ_MESSAGE"
            assert payload["chatId"] == CHAT_ID
            assert payload["messageId"] == "m1"
            assert isinstance(payload["mark"], int)
        finally:
            handle.loop_box.stop()

    def test_read_receipt_not_fired_for_filtered_packets(self, monkeypatch):
        """Own echo / foreign chat must NOT produce read marks."""
        monkeypatch.setattr(MaxMain, "_READ_RECEIPT_DELAY_S", 0.01)
        handle = make_listener(monkeypatch)
        client = handle.session["client"]
        handle.listener._register_on(client)
        try:
            dispatch_sync(
                handle, client, msg_packet(sender=MY_ID)
            )  # own echo
            dispatch_sync(
                handle, client, msg_packet(chat_id=CHAT_ID + 1)
            )  # foreign chat
            time.sleep(0.15)  # generous window for any wrongful invoke
            assert client.invokes == []
        finally:
            handle.loop_box.stop()


class TestFiltering:
    def dispatch_and_expect_nothing(self, monkeypatch, packet):
        handle = make_listener(monkeypatch)
        client = handle.session["client"]
        # Register directly (no listen() thread needed for pure filtering).
        handle.listener._register_on(client)
        dispatch_sync(handle, client, packet)
        time.sleep(0.15)  # generous window for any wrongful delivery
        assert handle.ingested == []
        handle.loop_box.stop()
        return handle

    def test_own_echo_sender_equals_my_id_not_passed(self, monkeypatch):
        self.dispatch_and_expect_nothing(
            monkeypatch, msg_packet(sender=MY_ID, text="echo")
        )

    def test_foreign_chat_id_not_passed(self, monkeypatch):
        self.dispatch_and_expect_nothing(
            monkeypatch, msg_packet(chat_id=43, text="alien")
        )

    def test_non_128_opcode_silently_ignored(self, monkeypatch, caplog):
        packet = {"ver": 11, "cmd": 0, "seq": 2, "opcode": 64, "payload": {}}
        with caplog.at_level(logging.WARNING, logger="Max.main"):
            self.dispatch_and_expect_nothing(monkeypatch, packet)
        assert not [
            rec.getMessage() for rec in caplog.records
        ], "non-128 packets must be ignored SILENTLY (no warnings)"

    @pytest.mark.parametrize(
        "packet",
        [
            {  # op=128 without payload.message entirely
                "opcode": 128,
                "payload": {"chatId": CHAT_ID},
            },
            {  # message present, text missing
                "opcode": 128,
                "payload": {"chatId": CHAT_ID, "message": {"sender": 1}},
            },
        ],
        ids=["no-message", "no-text"],
    )
    def test_malformed_payload_warning_without_exception(
        self, monkeypatch, caplog, packet
    ):
        handle = make_listener(monkeypatch)
        client = handle.session["client"]
        handle.listener._register_on(client)
        with caplog.at_level(logging.WARNING, logger="Max.main"):
            dispatch_sync(handle, client, packet)
        time.sleep(0.1)
        assert handle.ingested == []
        assert any(
            "dropped" in rec.getMessage() for rec in caplog.records
        ), [rec.getMessage() for rec in caplog.records]
        handle.loop_box.stop()

    def test_handler_survives_garbage_packet(self, monkeypatch, caplog):
        # Non-dict garbage → warning, ws-thread unaffected (next packet flows).
        handle = make_listener(monkeypatch)
        client = handle.session["client"]
        handle.listener._register_on(client)
        handle.listener._start_ingest_worker()
        with caplog.at_level(logging.WARNING, logger="Max.main"):
            dispatch_sync(handle, client, "total garbage")
            dispatch_sync(handle, client, msg_packet())
        wait_until(lambda: len(handle.ingested) == 1)
        assert any("non-dict" in rec.getMessage() for rec in caplog.records)
        handle.loop_box.stop()


def backoff_pauses(handle):
    """Recorder calls minus healthy-state supervisor polls (1.0 s each)."""
    return [
        c
        for c in handle.event.calls
        if c != MaxMain._SUPERVISOR_POLL_S
    ]


class TestReconnect:
    def test_drop_triggers_single_reconnect_with_backoff_pause(
        self, monkeypatch
    ):
        fresh = FakeClient()
        handle = make_listener(monkeypatch, clients=[fresh])
        dead = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(dead.callbacks))
            dead._recv_task = DeadTask()  # programmable drop

            wait_until(
                lambda: handle.session["client"] is fresh,
                message="client was never swapped after reconnect",
                timeout=5,
            )
            assert fresh.connect_calls == 1
            assert fresh.login_calls == 1
            assert len(fresh.callbacks) == 1  # re-registered on new client
            # Exactly ONE backoff pause of the base step before the retry.
            assert backoff_pauses(handle) == [MaxMain._RECONNECT_BASE_S]
        finally:
            handle.event.set()
            thread.join(timeout=2)
            handle.loop_box.stop()

    def test_five_failed_reconnects_terminal_error_quiet_exit(
        self, monkeypatch, caplog
    ):
        failures = [Exception(f"connection refused #{i}") for i in range(5)]
        handle = make_listener(monkeypatch, clients=failures)
        dead = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(dead.callbacks))
            dead._recv_task = DeadTask()

            # Terminal path: 5 attempts each preceded by a recorded pause.
            thread.join(timeout=5)
            assert not thread.is_alive(), "listener did not exit quietly"
            assert handle.built and len(handle.built) == 5  # NEW client per cycle
            assert handle.session["client"] is dead  # swap never happened
            # Full schedule 5→10→20→40 then capped 60 before the 5th try.
            assert backoff_pauses(handle) == [5.0, 10.0, 20.0, 40.0, 60.0]
            assert not handle.event.is_set(), "stop_event untouched"
        finally:
            handle.event.set()
            handle.loop_box.stop()

    def test_permanent_loss_logged_as_error(self, monkeypatch, caplog):
        failures = [Exception("network unreachable") for _ in range(5)]
        handle = make_listener(monkeypatch, clients=failures)
        dead = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(dead.callbacks))
            with caplog.at_level(logging.ERROR, logger="Max.main"):
                dead._recv_task = DeadTask()
                thread.join(timeout=5)
            assert not thread.is_alive()
        finally:
            handle.loop_box.stop()

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(
            "MAX connection lost permanently" in msg for msg in errors
        ), errors

    def test_successful_reconnect_resets_backoff_counter(self, monkeypatch):
        fresh = FakeClient()
        another_dead_then_fresh = FakeClient()
        handle = make_listener(
            monkeypatch,
            clients=[fresh, another_dead_then_fresh],
        )
        dead = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(dead.callbacks))
            dead._recv_task = DeadTask()
            wait_until(lambda: handle.session["client"] is fresh)
            # Second drop AFTER a successful reconnect: pause restarts at
            # base, proving the failure counter reset.
            fresh._recv_task = DeadTask()
            wait_until(
                lambda: handle.session["client"] is another_dead_then_fresh,
                timeout=5,
            )
            assert handle.event.calls.count(MaxMain._RECONNECT_BASE_S) == 2
            assert all(c <= 10.0 for c in handle.event.calls)
        finally:
            handle.event.set()
            thread.join(timeout=2)
            handle.loop_box.stop()


class TestIngestWorker:
    def test_chunks_delivered_in_fifo_order_single_consumer(self, monkeypatch):
        handle = make_listener(monkeypatch)
        client = handle.session["client"]

        # Deterministic FIFO check: enqueue many chunks through the
        # callback, then verify strict delivery order and that exactly
        # ONE worker thread exists (single-consumer rule).
        threads_before = set(threading.enumerate())
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(client.callbacks))
            for i in range(50):
                dispatch_sync(
                    handle, client, msg_packet(text=f"chunk-{i:02d}")
                )
            wait_until(
                lambda: len(handle.ingested) == 50,
                message="not all chunks delivered",
            )
            assert handle.ingested == [f"chunk-{i:02d}" for i in range(50)]
            # Exactly ONE worker thread started FOR THIS listener
            # (single-consumer rule; daemon workers of earlier tests may
            # still be winding down, so diff against a pre-start snapshot).
            workers = [
                t
                for t in threading.enumerate()
                if t.name == "max-listener-ingest"
                and t not in threads_before
            ]
            assert len(workers) == 1
        finally:
            handle.event.set()
            thread.join(timeout=2)
            handle.loop_box.stop()

    def test_ingester_exception_does_not_kill_worker(self, monkeypatch):
        handle = make_listener(monkeypatch)
        calls = {"n": 0}
        boom = {"armed": True}

        def flaky_ingester(text):
            calls["n"] += 1
            if boom["armed"]:
                raise RuntimeError("kernel hiccup")

        handle.listener.ingester = flaky_ingester
        client = handle.session["client"]
        thread = start_listen(handle)
        try:
            wait_until(lambda: bool(client.callbacks))
            dispatch_sync(handle, client, msg_packet(text="first"))
            wait_until(lambda: calls["n"] == 1)
            boom["armed"] = False
            dispatch_sync(handle, client, msg_packet(text="second"))
            wait_until(lambda: calls["n"] == 2)
            assert handle.ingested == []  # flaky ingester captured nothing
            assert handle.listener._ingest_thread.is_alive()
        finally:
            handle.event.set()
            thread.join(timeout=2)
            handle.loop_box.stop()
