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
session-holder dict consumed by the nested Sender/Listener. Packet
handling (Listener, task 5) and paced sending (Sender, task 6) are still
stubs.
"""

import asyncio
import logging
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
        """Outgoing-message worker with pacing (implemented in task 6).

        The overridden ``__init__`` accepts the extra ``session`` holder
        (allowed by the CryptoLayer docs §5.2 п.8); it stores the holder
        and keeps the addressing rule from task 3.
        """

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

        def send(self, text: str):
            raise NotImplementedError("send is implemented in plan task 6")

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
