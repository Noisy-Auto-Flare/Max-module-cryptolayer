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

Skeleton only: Sender/Listener bodies and session logic are implemented
in plan tasks 4–7.
"""

import logging

from base_module import BaseModule, Credential

logger = logging.getLogger(__name__)


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
        """Outgoing-message worker with pacing (implemented in task 6)."""

        def __init__(self, credentials, user_id):
            # ADDRESSING RULE: validate user_id non-empty; Chat ID credential
            # is the sole dialog address and always wins over user_id
            # (warning logged on mismatch — enforced again at parse time).
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
            raise NotImplementedError("Sender is implemented in plan task 6")

        def send(self, text: str):
            raise NotImplementedError("send is implemented in plan task 6")

    class Listener(BaseModule.Listener):
        """Incoming-packet handler with filtering (implemented in task 5)."""

        def __init__(self, credentials, ingester, user_id, stop_event):
            # Same addressing rule as Sender: user_id validated non-empty,
            # Chat ID credential is authoritative on mismatch.
            if not user_id:
                raise ValueError("user_id must be non-empty")
            super().__init__(credentials, ingester, user_id, stop_event)
            chat_id_cred = credentials[2] if len(credentials) > 2 else ""
            if chat_id_cred and str(chat_id_cred).strip() != str(user_id).strip():
                logger.warning(
                    "user_id (%r) differs from Chat ID credential (%r); "
                    "Chat ID wins as the sole dialog address",
                    user_id,
                    chat_id_cred,
                )
            raise NotImplementedError("Listener is implemented in plan task 5")

        def listen(self) -> str:
            raise NotImplementedError("listen is implemented in plan task 5")

    def create_session(self, ingester):
        """Create the MAX session and wire Sender/Listener (task 4).

        Contract: takes EXACTLY one argument (``ingester``).
        """
        raise NotImplementedError("create_session is implemented in plan task 4")
