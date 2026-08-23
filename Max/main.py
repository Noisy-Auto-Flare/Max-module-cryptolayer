"""MAX transport module for CryptoLayer.

Implements the CryptoLayer ``BaseModule`` contract (module
``base_module`` from ``cryptolayer-module-interface``): transport of text
messages through a personal MAX account via the unofficial user-API
library ``vkmax`` (see ``docs/vkmax-contract.md`` — source of truth for
all vkmax adapter names, pinned in task 2).

ADDRESSING RULE (fixed since task 3):
    The dialog address is SOLELY the credential «Chat ID» — MAX dialogs
    are addressed by chat_id. The constructor argument ``user_id`` is
    validated non-empty; if it differs from Chat ID, a warning is logged
    and **Chat ID wins** as the single authoritative address.

Session adapter (task 4): ``create_session(ingester)`` runs a dedicated
daemon thread hosting an asyncio event loop (ProactorEventLoop on Windows
is fine — contract §12), logs in by token inside that loop and stores a
session-holder dict consumed by the nested Sender/Listener.

Sender (task 6): ``send(text)`` is ALWAYS non-blocking — it puts into a
bounded ``queue.Queue(maxsize=1000)`` via ``put_nowait()`` (Full → warning
+ immediate drop; the CryptoLayer kernel transport retries packets itself).
A daemon worker thread takes items one by one, waits the human-pacing
pause through the SINGLE interruptible primitive ``stop_event.wait(
random.uniform(min_pause, max_pause))`` (no ``time.sleep`` anywhere),
then dispatches ``send_message(client, chat_id, text)`` into the session
loop under ``asyncio.wait_for`` (contract §9/§7). Errors are classified:
transient (network/timeout/None-result/flood markers) → exponential
backoff base 5 s cap 120 s, retry while the stop_event stays clear;
terminal (auth/permission/bad-chat-id markers, or 5 consecutive failures
of one message when the text is unclassifiable) → ERROR log with an
actionable message («проверьте Token / Chat ID»), queue cleared, worker
stops WITHOUT touching stop_event. Typing emulation is NOT implemented —
contract §12 verdict NO (ADR-3a, documented in task 10).
Packet handling (Listener, task 5) is still a stub.
"""

import asyncio
import logging
import queue
import random
import threading

from base_module import BaseModule, Credential

logger = logging.getLogger(__name__)

# Contract §7: vkmax invoke_method waits for the server answer with NO
# timeout at all — every call is therefore wrapped in asyncio.wait_for,
# and the cross-thread future gets its own belt-and-suspenders timeout.
_LOGIN_TIMEOUT_S = 30.0
_RESULT_TIMEOUT_S = 45.0
_THREAD_JOIN_TIMEOUT_S = 5.0

_DEFAULT_MIN_PAUSE_S = 2.0
_DEFAULT_MAX_PAUSE_S = 6.0


def _parse_pause(raw, default: float, label: str) -> float:
    """Parse a pause credential (float seconds).

    Empty/blank value → ``default``; unparsable garbage → warning log +
    ``default`` (pacing must never block session bring-up).
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return default
    try:
        return float(text.replace(",", "."))
    except ValueError:
        logger.warning(
            "%s: %r is not a number; using default %.1f sec", label, text, default
        )
        return default


def _build_client(token: str, device_id: str):
    """Build a vkmax client (adapter factory, contract §13).

    Thin wrapper over ``vkmax.client.MaxClient()`` (constructor takes NO
    arguments — see docs/vkmax-contract.md §0/§1). This function is THE
    monkeypatch point for tests.

    NOTE: the ``vkmax`` import is deliberately LAZY (inside this body) so
    that importing ``Max.main`` never requires vkmax to be installed —
    discovery (module_manager CLI) imports every module's main.py, and a
    missing optional dependency must not break it.
    """
    from vkmax.client import MaxClient  # lazy import on purpose

    return MaxClient()


# --- Sender (task 6): pacing, error classification, bounded queue ---

_SEND_TIMEOUT_S = 45.0  # asyncio.wait_for around send_message (contract §7)
_WORKER_POLL_S = 0.1  # idle poll of the queue while waiting for work
_BACKOFF_BASE_S = 5.0  # exponential backoff base (plan task 6)
_BACKOFF_CAP_S = 120.0  # exponential backoff cap (plan task 6)
_MAX_CONSECUTIVE_FAILURES = 5  # unclassifiable errors → terminal after N

# Contract §8: vkmax raises GENERIC Exception(str) — no error classes.
# Classification is therefore text-based; terminal markers first, then
# transient, anything else is "unknown" and falls back to the
# consecutive-failure rule. Exact server texts get pinned at the live
# owner checkpoint (task 12); until then this stays conservative.
_TERMINAL_MARKERS = (
    "invalid token",
    "token",  # covers bad/missing/expired token
    "auth",
    "unauthorized",
    "permission",
    "denied",
    "forbidden",
    "not found",
    "nosuchchat",
    "chatid",  # nonexistent dialog address
)
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connectionclosed",
    "connection closed",
    "connectionreset",
    "connection refused",
    "flood",
    "ratelimit",
    "rate limit",
)


class _TransientSendError(Exception):
    """Internal: ``send_message`` returned None (= not sent, §7)."""


def _get_send_message():
    """Return the vkmax send function lazily (contract §9).

    Lazy import keeps ``import Max.main`` working without vkmax
    installed; cached after the first call. THE monkeypatch point for
    sender tests.
    """
    global _SEND_MESSAGE_FN
    if _SEND_MESSAGE_FN is None:
        from vkmax.functions.messages import send_message

        _SEND_MESSAGE_FN = send_message
    return _SEND_MESSAGE_FN


_SEND_MESSAGE_FN = None


def _classify_send_error(exc: BaseException) -> str:
    """Classify a send failure: ``"transient"`` / ``"terminal"`` /
    ``"unknown"``.

    Timeouts are transient by type regardless of message text (contract
    §7); string markers per §8 above; everything else is unknown and is
    retried only up to the consecutive-failure limit.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "transient"
    text = str(exc).lower()
    if any(marker in text for marker in _TERMINAL_MARKERS):
        return "terminal"
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


class Max(BaseModule):
    """CryptoLayer transport module: messaging over personal MAX accounts."""

    @property
    def unique_id(self) -> str:
        return "max_user_1"

    @property
    def name(self) -> str:
        return "MAX"

    @property
    def description(self) -> str:
        return (
            "Транспорт через личный аккаунт MAX (неофициальный user-API, "
            "vkmax). ВНИМАНИЕ: нарушает ToS MAX"
        )

    expected_credentials = [
        Credential("Token", "…__oneme_auth из LocalStorage web.max.ru"),
        Credential("Device ID", "…__oneme_device_id"),
        Credential(
            "Chat ID",
            "числовой id диалога с собеседником (см. scripts/discover_chats.py)",
        ),
        Credential("Мин. пауза, сек", "пауза между отправками; пусто = 2"),
        Credential("Макс. пауза, сек", "пусто = 6"),
    ]

    class Sender(BaseModule.Sender):
        """Outgoing-message worker with pacing (task 6).

        The overridden ``__init__`` accepts the extra ``session`` holder
        (allowed by the CryptoLayer docs §5.2 п.8); it stores the holder
        and keeps the addressing rule from task 3.

        TYPING EMULATION: contract §12 verdict NO — vkmax has no
        typing/status opcode, so typing is NOT implemented and NOT sent
        before messages (decision recorded as ADR-3a in docs/DECISIONS.md,
        written in plan task 10).
        """

        # Bounded queue per plan task 6; class attr so tests can shrink it.
        _QUEUE_MAXSIZE = 1000

        def __init__(self, credentials, user_id, session):
            # ADDRESSING RULE: validate user_id non-empty; Chat ID credential
            # is the sole dialog address and always wins over user_id
            # (warning logged on mismatch).
            if not user_id:
                raise ValueError("user_id must be non-empty")
            super().__init__(credentials, user_id)
            chat_id_cred = credentials[2] if len(credentials) > 2 else ""
            if chat_id_cred and str(chat_id_cred).strip() != str(user_id).strip():
                logger.warning(
                    "user_id (%r) differs from Chat ID credential (%r); "
                    "Chat ID wins as the sole dialog address",
                    user_id,
                    chat_id_cred,
                )
            self.session = session
            self._queue = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
            self._worker_stop = threading.Event()
            self._worker_finished = threading.Event()
            self._worker_thread = None

        def send(self, text: str):
            """Enqueue a message — ALWAYS non-blocking.

            Never sends directly: the paced worker owns the transport.
            On a full queue the message is dropped with a warning — the
            CryptoLayer kernel transport retries packets itself.
            """
            self._ensure_worker()
            try:
                self._queue.put_nowait(str(text))
            except queue.Full:
                logger.warning(
                    "MAX sender queue is full (%d); dropping message",
                    self._QUEUE_MAXSIZE,
                )

        # --- worker lifecycle -------------------------------------------------

        def request_stop(self):
            """Cooperative stop signal for the sender worker only.

            Task 7 wires the FULL graceful shutdown (poison pill + client
            disconnect) on top of this primitive; the shared module
            ``stop_event`` is intentionally NOT touched here.
            """
            self._worker_stop.set()

        def join_worker(self, timeout=None):
            """Join the worker thread if it was ever started."""
            thread = self._worker_thread
            if thread is not None:
                thread.join(timeout)

        @property
        def worker_stopped(self) -> bool:
            return self._worker_finished.is_set()

        def _ensure_worker(self):
            """Start the daemon worker lazily on the first send()."""
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_stop.clear()
            self._worker_finished.clear()
            self._worker_thread = threading.Thread(
                target=self._run_worker,
                name="max-sender-worker",
                daemon=True,
            )
            self._worker_thread.start()

        def _run_worker(self):
            stop_event = self.session["stop_event"]
            try:
                while not self._worker_stop.is_set():
                    must_exit = self._process_one(stop_event)
                    if must_exit:
                        break
            finally:
                self._worker_finished.set()

        def _process_one(self, stop_event) -> bool:
            """Take one queued message and send it with pacing/retries.

            Returns True when the worker must exit (stop requested or a
            terminal failure), False to keep looping.
            """
            try:
                text = self._queue.get(timeout=_WORKER_POLL_S)
            except queue.Empty:
                return False

            # Human-pacing jitter through the SINGLE interruptible
            # primitive (no time.sleep anywhere in waiting paths).
            pause = random.uniform(
                self.session["min_pause"], self.session["max_pause"]
            )
            if stop_event.wait(pause):
                logger.info(
                    "Sender: stop_event during pacing pause; dropping "
                    "message plus %d queued",
                    self._queue.qsize(),
                )
                self._clear_queue()
                return True

            backoff = _BACKOFF_BASE_S
            consecutive = 0
            while True:
                try:
                    result = self._send_via_loop(text)
                    if result is None:
                        raise _TransientSendError(
                            "send_message returned None "
                            "(server silence = not sent)"
                        )
                    logger.debug("Sender: message delivered")
                    return False  # success — per-message counter resets too
                except Exception as exc:  # noqa: BLE001 — vkmax raises generic
                    consecutive += 1
                    if isinstance(exc, _TransientSendError):
                        kind = "transient"  # None result = §7 «не отправлено»
                    else:
                        kind = _classify_send_error(exc)
                    terminal = kind == "terminal" or (
                        kind == "unknown"
                        and consecutive >= _MAX_CONSECUTIVE_FAILURES
                    )
                    if terminal:
                        logger.error(
                            "Sender: TERMINAL send failure after %d attempt(s) "
                            "(%s: %s) — остановка worker'а. Проверьте Token / "
                            "Chat ID в credentials модуля.",
                            consecutive,
                            type(exc).__name__,
                            exc,
                        )
                        self._clear_queue()
                        self.request_stop()
                        return True
                    delay = min(backoff, _BACKOFF_CAP_S)
                    logger.warning(
                        "Sender: transient send failure (%s: %s); retrying "
                        "in %.1f s (attempt %d)",
                        type(exc).__name__,
                        exc,
                        delay,
                        consecutive,
                    )
                    # Backoff is interruptible by the shared stop_event.
                    if stop_event.wait(delay):
                        logger.info(
                            "Sender: stop_event during backoff; dropping "
                            "message plus %d queued",
                            self._queue.qsize(),
                        )
                        self._clear_queue()
                        return True
                    backoff = min(backoff * 2, _BACKOFF_CAP_S)

        def _send_via_loop(self, text):
            """Dispatch one send into the session's asyncio loop.

            Contract §9/§7: ``send_message(client, chat_id, text)``
            (op 64), wrapped in ``asyncio.wait_for`` because vkmax's
            invoke_method has NO internal timeout.
            """
            session = self.session
            client = session["client"]
            loop = session["loop"]
            chat_id = session["chat_id"]
            send_message = _get_send_message()

            async def _call():
                return await asyncio.wait_for(
                    send_message(client, chat_id, text),
                    timeout=_SEND_TIMEOUT_S,
                )

            future = asyncio.run_coroutine_threadsafe(_call(), loop)
            return future.result(_RESULT_TIMEOUT_S + 1.0)

        def _clear_queue(self):
            """Drain every pending message without sending them."""
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    return

    class Listener(BaseModule.Listener):
        """Incoming-packet handler with filtering (implemented in task 5).

        Same overridden-``__init__`` contract as Sender; additionally
        holds ``ingester`` (via the base class) and takes its stop_event
        from the session holder (single shared shutdown signal).
        """

        def __init__(self, credentials, ingester, user_id, session):
            # Same addressing rule as Sender: user_id validated non-empty,
            # Chat ID credential is authoritative on mismatch.
            if not user_id:
                raise ValueError("user_id must be non-empty")
            super().__init__(
                credentials, ingester, user_id, session["stop_event"]
            )
            chat_id_cred = credentials[2] if len(credentials) > 2 else ""
            if chat_id_cred and str(chat_id_cred).strip() != str(user_id).strip():
                logger.warning(
                    "user_id (%r) differs from Chat ID credential (%r); "
                    "Chat ID wins as the sole dialog address",
                    user_id,
                    chat_id_cred,
                )
            self.session = session

        def listen(self) -> str:
            raise NotImplementedError("listen is implemented in plan task 5")

    def _stop_loop_thread(self) -> None:
        """Stop the dedicated event-loop thread.

        Used on the create_session failure paths here; the full graceful
        shutdown (worker stop + client disconnect first) arrives in task 7
        on top of this primitive.
        """
        loop = getattr(self, "_loop", None)
        thread = getattr(self, "_loop_thread", None)
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # loop already closed between check and call
        if thread is not None and thread.is_alive():
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
        if loop is not None and not loop.is_closed():
            loop.close()

    def create_session(self, ingester):
        """Create the MAX session and wire Sender/Listener (task 4).

        Contract: takes EXACTLY one argument (``ingester``).

        Steps: parse credentials → spawn a daemon thread running a fresh
        asyncio event loop → inside that loop build the vkmax client,
        connect and log in by token → read our own id from the login
        response → store the session-holder dict → instantiate the nested
        Listener (holding ``ingester``) and Sender.

        Login failure of any kind raises ``RuntimeError("MAX login "
        "failed: проверьте Token и Device ID")`` with the original error
        as ``__cause__``. An unreadable own id is a separate hard failure
        (echo filtering in task 5 depends on it): RuntimeError, no
        degraded mode.
        """
        credentials = list(getattr(self, "credentials", None) or [])
        if len(credentials) < 3:
            raise ValueError(
                "module has no credentials: call init(creds, user_id) first"
            )
        token = str(credentials[0]).strip()
        device_id = str(credentials[1]).strip()

        raw_chat_id = str(credentials[2]).strip()
        try:
            chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise ValueError(
                f"Chat ID must be an integer, got {raw_chat_id!r}"
            ) from exc

        user_id = getattr(self, "user_id", "")
        if not user_id:
            # Fail BEFORE spawning any thread — no resources to leak.
            raise ValueError("user_id must be non-empty")

        min_pause = _parse_pause(
            credentials[3] if len(credentials) > 3 else "",
            _DEFAULT_MIN_PAUSE_S,
            "Мин. пауза, сек",
        )
        max_pause = _parse_pause(
            credentials[4] if len(credentials) > 4 else "",
            _DEFAULT_MAX_PAUSE_S,
            "Макс. пауза, сек",
        )

        # --- threaded asyncio bridge (Telegram-module pattern) ---
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="max-asyncio-loop",
            daemon=True,
        )
        self._loop_thread.start()

        def _run(coro):
            return asyncio.run_coroutine_threadsafe(
                coro, self._loop
            ).result(_RESULT_TIMEOUT_S)

        async def _login():
            # Contract §2: connect() MUST precede login (@ensure_connected
            # raises otherwise); login_by_token RETURNS the full server
            # response (§1.1/§2), so profile.id comes straight out of it.
            client = _build_client(token, device_id)
            await client.connect()
            response = await asyncio.wait_for(
                client.login_by_token(token, device_id),
                timeout=_LOGIN_TIMEOUT_S,
            )
            return client, response

        try:
            client, login_response = _run(_login())
        except Exception as exc:
            self._stop_loop_thread()
            raise RuntimeError(
                "MAX login failed: проверьте Token и Device ID"
            ) from exc

        # Own id — HARD requirement for sender-echo filtering (task 5).
        # Contract §5: client.me does NOT exist; payload.profile.id of the
        # login response is the ONLY source. Unreadable → RuntimeError.
        try:
            my_id = login_response["payload"]["profile"]["id"]
        except (KeyError, TypeError) as exc:
            self._stop_loop_thread()
            raise RuntimeError(
                "MAX login failed: my id unavailable "
                "(payload.profile.id missing in login response)"
            ) from exc

        self._session = {
            "client": client,
            "loop": self._loop,
            "chat_id": chat_id,
            "my_id": my_id,
            "stop_event": self.stop_event,
            "min_pause": min_pause,
            "max_pause": max_pause,
        }
        self.listener = self.Listener(
            list(self.credentials), ingester, user_id, self._session
        )
        self.sender = self.Sender(list(self.credentials), user_id, self._session)
