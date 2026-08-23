"""Task 7: graceful shutdown wired to the shared stop_event.

Deterministic: FakeClient's disconnect() is programmable (ok / raise);
every join uses an explicit timeout; no real sleep exceeds 1 s. The
vkmax send function is monkeypatched at ``Max.main._SEND_MESSAGE_FN``
(the module-level cache behind ``_get_send_message``).
"""

import importlib
import sys
import threading

import pytest

sys.path.insert(0, ".")

MaxMain = importlib.import_module("Max.main")

_JOIN_S = 5.0  # plan-mandated bound: everything finishes <5 s


class FakeClient:
    """Stand-in for vkmax MaxClient with a programmable disconnect()."""

    def __init__(self, disconnect_error=None):
        self.disconnect_calls = 0
        self.disconnect_error = disconnect_error
        self.login_response = {"payload": {"profile": {"id": 777}}}

    async def connect(self):
        return None

    async def login_by_token(self, token, device_id=None):
        return self.login_response

    def set_packet_callback(self, cb):  # sync registration (contract §3)
        self.registered_cb = cb

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error


@pytest.fixture
def make_module(monkeypatch):
    """Build a sessioned Max() with FakeClient + instant-send plumbing."""
    created = []
    monkeypatch.setattr(MaxMain, "_SEND_MESSAGE_FN", lambda *a: {"ok": True})

    def _make(client=None, creds=None):
        # BaseModule.stop_event is a CLASS-level Event shared by every
        # instance — a previous test may leave it set, which would fire
        # this module's watcher instantly. Reset it per module.
        MaxMain.Max.stop_event.clear()
        client = FakeClient() if client is None else client

        def _fake_build(token, device_id):
            return client

        monkeypatch.setattr(MaxMain, "_build_client", _fake_build)
        mod = MaxMain.Max()
        mod.init(
            ["tok", "dev", "123", "0.01", "0.02"] if creds is None else creds,
            "123",
        )
        mod.create_session(lambda text: None)
        created.append((mod, client))
        return mod, client

    yield _make

    for mod, _client in created:
        mod.stop_event.set()
        mod._shutdown()
    MaxMain.Max.stop_event.clear()  # shared class-level event (see _make)


def _start_listener(mod):
    t = threading.Thread(
        target=mod.listener.listen, name="test-listener", daemon=True
    )
    t.start()
    return t


def _module_threads(mod):
    threads = [mod._loop_thread, mod._stop_watcher]
    if mod.sender is not None and mod.sender._worker_thread is not None:
        threads.append(mod.sender._worker_thread)
    if mod.listener._ingest_thread is not None:
        threads.append(mod.listener._ingest_thread)
    return [t for t in threads if t is not None]


class TestHappyPath:
    def test_all_module_threads_finish_within_5s_after_stop_event(
        self, make_module
    ):
        mod, client = make_module()
        listener_thread = _start_listener(mod)

        # Start the pacing worker and give it one deliverable message.
        mod.sender.send("hello")
        assert mod.sender._worker_thread is not None
        mod.sender._worker_thread.join(timeout=2.0)  # fast pauses → done

        mod.stop_event.set()

        deadline_threads = _module_threads(mod) + [listener_thread]
        for thread in deadline_threads:
            thread.join(timeout=_JOIN_S)
            assert not thread.is_alive(), f"{thread.name} did not finish"

        assert client.disconnect_calls == 1
        assert not mod._loop.is_running()
        assert mod._loop.is_closed()

    def test_stop_method_sets_event_and_shuts_down(self, make_module):
        mod, client = make_module()
        mod.sender.send("hi")  # spawn worker
        mod.listener._start_ingest_worker()

        mod.stop()

        for thread in _module_threads(mod):
            thread.join(timeout=_JOIN_S)
            assert not thread.is_alive(), f"{thread.name} did not finish"
        assert client.disconnect_calls == 1

    def test_repeat_stop_is_noop_idempotent(self, make_module):
        mod, client = make_module()
        mod.stop()
        first_count = client.disconnect_calls

        mod.stop()  # second call — must be a NO-OP
        mod.stop()

        assert client.disconnect_calls == first_count
        for thread in _module_threads(mod):
            assert not thread.is_alive()


class TestFailurePath:
    def test_disconnect_raising_still_completes_and_logs(
        self, make_module, caplog
    ):
        boom = Exception("Keepalive task is not running")
        mod, client = make_module(client=FakeClient(disconnect_error=boom))
        mod.sender.send("msg")
        mod.listener._start_ingest_worker()

        with caplog.at_level("WARNING"):
            mod.stop()

        assert client.disconnect_calls == 1  # attempted exactly once
        for thread in _module_threads(mod):
            thread.join(timeout=_JOIN_S)
            assert not thread.is_alive(), f"{thread.name} did not finish"
        assert not mod._loop.is_running()
        # The failure IS logged, but shutdown completed anyway.
        assert any(
            "disconnect" in rec.message.lower() and rec.levelno >= 30
            for rec in caplog.records
        )

    def test_watcher_on_stop_event_triggers_shutdown_with_raising_disconnect(
        self, make_module
    ):
        mod, client = make_module(
            client=FakeClient(disconnect_error=Exception("already closed"))
        )
        watcher = mod._stop_watcher

        mod.stop_event.set()  # watcher-driven path

        watcher.join(timeout=_JOIN_S)
        assert not watcher.is_alive()
        for thread in _module_threads(mod):
            thread.join(timeout=_JOIN_S)
            assert not thread.is_alive()

    def test_shutdown_with_already_closed_loop_no_unawaited_warning(
        self, make_module, recwarn
    ):
        """Dead-loop teardown: no 'never awaited' RuntimeWarning escapes."""
        mod, client = make_module()

        # Simulate the loop dying BEFORE shutdown (race / prior teardown).
        mod._loop.call_soon_threadsafe(mod._loop.stop)
        mod._loop_thread.join(timeout=_JOIN_S)
        assert not mod._loop_thread.is_alive()
        mod._loop.close()  # stop alone leaves it open; task-4 closes it
        assert mod._loop.is_closed()

        mod.stop()  # must complete cleanly, skipping disconnect

        for thread in _module_threads(mod):
            thread.join(timeout=_JOIN_S)
            assert not thread.is_alive(), f"{thread.name} did not finish"
        assert client.disconnect_calls == 0  # never even attempted
        never_awaited = [
            w for w in recwarn
            if issubclass(w.category, RuntimeWarning)
            and "never awaited" in str(w.message)
        ]
        assert never_awaited == []
