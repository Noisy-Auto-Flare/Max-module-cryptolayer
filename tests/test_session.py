"""Task 4: session adapter — threaded asyncio bridge, token login, wiring.

All network behaviour is replaced by FakeClient (plain class, async
methods returning canned data); the vkmax factory ``Max.main._build_client``
is monkeypatched, so nothing here touches the real protocol.
"""

import asyncio
import importlib
import inspect
import sys
import threading
import time

import pytest

sys.path.insert(0, ".")

MaxMain = importlib.import_module("Max.main")


class FakeClient:
    """Deterministic stand-in for vkmax MaxClient (contract §1/§2).

    Mirrors only what task 4 uses: connect() must precede login
    (@ensure_connected), login_by_token(token, device_id) returns the
    FULL server response and may raise a generic Exception on bad auth.
    """

    def __init__(self, login_response=None, login_error=None):
        self.connect_calls = 0
        self.login_calls = []
        self.login_response = (
            {"payload": {"profile": {"contact": {"id": 777}}}}
            if login_response is None
            else login_response
        )
        self.login_error = login_error
        self.packet_callback = None
        self._recv_task = None  # duck-typed by Listener._ws_dead
        self.invokes = []  # (opcode, payload) recorder for raw invokes

    async def connect(self):
        self.connect_calls += 1

    async def disconnect(self):  # used by later tasks; kept for symmetry
        return None

    async def login_by_token(self, token, device_id=None):
        self.login_calls.append((token, device_id))
        if self.login_error is not None:
            raise self.login_error
        return self.login_response

    def set_packet_callback(self, function):  # contract §3: sync registration
        self.packet_callback = function

    async def invoke_method(self, opcode=0, payload=None, retries=2):
        self.invokes.append((opcode, payload))
        return None


@pytest.fixture
def make_module(monkeypatch):
    """Build an inited Max() with _build_client patched to a FakeClient."""
    created = []

    def _make(creds=None, user_id="123", client=None):
        client = FakeClient() if client is None else client
        monkeypatch.setattr(MaxMain, "_build_client", lambda t, d: client)
        mod = MaxMain.Max()
        mod.init(
            ["tok", "dev", "123", "", ""] if creds is None else creds,
            user_id,
        )
        created.append(mod)
        return mod, client

    yield _make

    # Hygiene: never leak a running loop thread across tests.
    for mod in created:
        mod._stop_loop_thread()


class TestSignature:
    def test_create_session_takes_exactly_self_and_ingester(self):
        params = list(inspect.signature(MaxMain.Max.create_session).parameters)
        assert params == ["self", "ingester"]


class TestHappyPath:
    def test_session_holder_populated_and_login_called_with_our_creds(
        self, make_module
    ):
        mod, client = make_module()
        mod.create_session(lambda text: None)

        assert client.connect_calls == 1
        assert client.login_calls == [("tok", "dev")]

        holder = mod._session
        assert holder["client"] is client
        assert holder["loop"] is mod._loop
        assert holder["chat_id"] == 123
        assert holder["my_id"] == 777
        assert holder["stop_event"] is mod.stop_event
        assert holder["min_pause"] == 2.0
        assert holder["max_pause"] == 6.0

    def test_sender_and_listener_created_listener_holds_ingester(
        self, make_module
    ):
        ingester_calls = []

        def ingester(text):
            ingester_calls.append(text)

        mod, _client = make_module()
        mod.create_session(ingester)

        assert isinstance(mod.sender, MaxMain.Max.Sender)
        assert isinstance(mod.listener, MaxMain.Max.Listener)
        assert mod.listener.ingester is ingester
        assert mod.sender.session is mod._session
        assert mod.listener.session is mod._session

    def test_loop_thread_is_dedicated_daemon(self, make_module):
        threads_before = set(threading.enumerate())
        mod, _client = make_module()
        mod.create_session(lambda text: None)

        thread = mod._loop_thread
        assert thread is not None and thread.is_alive()
        assert thread.daemon is True
        assert thread.name == "max-asyncio-loop"
        assert thread not in threads_before

    @pytest.mark.parametrize(
        ("raw_min", "raw_max", "exp_min", "exp_max"),
        [
            ("3.5", "9.25", 3.5, 9.25),  # explicit values parsed
            ("", "", 2.0, 6.0),  # empty -> defaults
            ("   ", None, 2.0, 6.0),  # blank/None -> defaults
            ("garbage", "1,5", 2.0, 1.5),  # junk -> default; comma ok
        ],
        ids=["explicit", "empty-defaults", "blank-defaults", "junk-fallback"],
    )
    def test_pause_parsing_and_chat_id_int(
        self, make_module, raw_min, raw_max, exp_min, exp_max
    ):
        mod, _client = make_module(
            creds=["t", "d", "555", raw_min, raw_max]
        )
        mod.create_session(lambda text: None)

        assert mod._session["chat_id"] == 555
        assert mod._session["min_pause"] == exp_min
        assert mod._session["max_pause"] == exp_max


class TestFailures:
    def test_login_error_wrapped_runtime_error_with_cause(self, make_module):
        boom = Exception("AUTH_FAILED: invalid token")
        mod, _client = make_module(client=FakeClient(login_error=boom))

        with pytest.raises(
            RuntimeError, match="MAX login failed: проверьте Token и Device ID"
        ) as excinfo:
            mod.create_session(lambda text: None)

        assert excinfo.value.__cause__ is boom
        mod._loop_thread.join(timeout=2)
        assert not mod._loop_thread.is_alive()

    @pytest.mark.parametrize(
        "bad_response",
        [
            {"payload": {}},  # no profile
            {"payload": {"profile": {}}},  # profile without id
            {"unexpected": True},  # no payload at all
            "not-a-dict",  # string response: TypeError path
        ],
        ids=["no-profile", "no-id", "no-payload", "non-dict"],
    )
    def test_unreadable_my_id_raises_runtime_error_no_degraded_mode(
        self, make_module, bad_response
    ):
        mod, _client = make_module(client=FakeClient(login_response=bad_response))

        with pytest.raises(RuntimeError, match="my id unavailable"):
            mod.create_session(lambda text: None)

        mod._loop_thread.join(timeout=2)
        assert not mod._loop_thread.is_alive()

    def test_my_id_fallback_legacy_profile_id_path(self, make_module):
        """Legacy payload.profile.id still works when contact.id is absent."""
        mod, _client = make_module(
            client=FakeClient(
                login_response={"payload": {"profile": {"id": 555}}}
            )
        )
        mod.create_session(lambda text: None)
        assert mod._session["my_id"] == 555
        mod.stop()

    def test_create_session_auto_starts_listener_thread(self, make_module):
        """REGRESSION (live ping-timeout bug): the kernel wires only the
        sender, so create_session itself must start the listener thread —
        which registers the packet callback on the client. Without the
        auto-start nothing is ever received (sending still works, so the
        failure mode is a one-way transport + kernel ping timeout)."""
        mod, client = make_module()
        # BaseModule.stop_event is CLASS-level and shared across instances:
        # a previous test's stop() leaves it set, which would make the
        # listener thread exit instantly. Reset for a clean slate.
        mod.stop_event.clear()
        mod.create_session(lambda text: None)
        try:
            # The listener thread is up...
            assert mod._listener_thread.is_alive()
            assert mod._listener_thread.name == "max-listener"
            # ...and registration happened WITHOUT anyone calling listen().
            deadline = time.monotonic() + 2.0
            while client.packet_callback is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.packet_callback is not None
        finally:
            mod.stop()
        assert not mod._listener_thread.is_alive()

    def test_online_presence_armed_after_login(self, make_module):
        """Contract §14: after create_session the client's keepalive must
        be patched to report {"interactive": True} («в сети»), instead of
        the library default {"interactive": False} («не в сети»)."""
        mod, client = make_module()
        mod.stop_event.clear()
        mod.create_session(lambda text: None)
        try:
            patched = getattr(client, "_send_keepalive_packet", None)
            assert patched is not None
            # The patch is an INSTANCE attribute shadowing the class method.
            assert "MaxClient" not in repr(patched)
            # Invoking it must fire opcode 1 with interactive=True.
            asyncio.run(patched())
            assert (1, {"interactive": True}) in client.invokes
            assert (1, {"interactive": False}) not in client.invokes
        finally:
            mod.stop()

    def test_no_thread_leak_after_failure(self, make_module):
        before = threading.active_count()
        mod, _client = make_module(client=FakeClient(login_error=Exception("x")))

        with pytest.raises(RuntimeError, match="MAX login failed"):
            mod.create_session(lambda text: None)

        assert threading.active_count() == before
        mod._stop_loop_thread()  # idempotent second stop is safe
        assert threading.active_count() == before

    def test_empty_user_id_rejected_before_any_thread_spawn(self, make_module):
        mod, _client = make_module(user_id="")

        with pytest.raises(ValueError, match="user_id must be non-empty"):
            mod.create_session(lambda text: None)

        assert getattr(mod, "_loop", None) is None
        assert getattr(mod, "_loop_thread", None) is None

    def test_non_integer_chat_id_rejected(self, make_module):
        mod, _client = make_module(creds=["t", "d", "not-a-number", "", ""])

        with pytest.raises(ValueError, match="Chat ID"):
            mod.create_session(lambda text: None)

        assert getattr(mod, "_loop", None) is None

    def test_create_session_without_init_raises_value_error(self):
        mod = MaxMain.Max()

        with pytest.raises(ValueError, match="credentials"):
            mod.create_session(lambda text: None)
