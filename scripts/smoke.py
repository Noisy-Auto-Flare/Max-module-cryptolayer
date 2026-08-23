#!/usr/bin/env python3
"""smoke.py — doc-canonical manual smoke checklist + optional live send test.

Part 1 (offline): prints the step-by-step manual-check checklist per the canon
of cryptolayer docs §5.3: copy Max/ into the CLI's src/modules, run
generate_reqs.py, pip install common_requirements.txt, launch the CLI — with an
explicit WARNING that `./run.sh` and `git submodule update --init --recursive`
are FORBIDDEN after copying (they wipe foreign modules / reset submodules).

Part 2 (live, opt-in): `--send-test "текст"` performs a one-off send outside
CryptoLayer per docs/vkmax-contract.md (§2 connect/login lifecycle, §6 shutdown,
§9 send_message op=64 under asyncio.wait_for). Requires --token, --device-id
and --chat-id; exits with code 2 if any is missing.

Tokens and device ids are MASKED in all output (only last 4 chars shown).
This script is a development helper — NOT part of the Max/ module folder.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# --- masking ----------------------------------------------------------------


def mask(secret: str | None) -> str:
    """Return masked form of a secret: only its last 4 characters visible."""
    if not secret:
        return "<empty>"
    tail = secret[-4:]
    return f"***{tail}"


# --- offline checklist ------------------------------------------------------


def print_checklist() -> None:
    print("=" * 72)
    print("SMOKE-ЧЕКЛИСТ ручной проверки модуля MAX (канон docs README §5.3)")
    print("=" * 72)
    print()
    print("Шаг 1. Скопируйте папку модуля в приложение CryptoLayer:")
    print("           cp -r Max/ <cryptolayer-cli>/src/modules/Max/")
    print()
    print("Шаг 2. Сгенерируйте requirements для ядра:")
    print("           python3 src/modules/generate_reqs.py")
    print()
    print("Шаг 3. Установите общие зависимости:")
    print("           pip install -r src/modules/common_requirements.txt")
    print()
    print("Шаг 4. Запустите CLI и добавьте модуль MAX с credentials:")
    print("           Token       = __oneme_auth   из LocalStorage web.max.ru")
    print("           Device ID   = __oneme_device_id оттуда же")
    print("           Chat ID     = числовой id диалога (discover_chats.py)")
    print("           Мин./Макс. пауза = пусто (дефолты 2–6 c)")
    print()
    print("-" * 72)
    print("!!! ВНИМАНИЕ !!! После копирования модуля ЗАПРЕЩЕНО запускать:")
    print("    ./run.sh                              # пересоберёт и СТЁТ чужие модули")
    print("    git submodule update --init --recursive   # сбросит submodule")
    print("Эти команды уничтожат скопированный модуль — pip install из шага 3.")
    print("-" * 72)
    print()
    print("Ожидаемое поведение после запуска:")
    print("  * create_session логинится по токену (op 19) и читает profile.id;")
    print("  * входящие доходят до ядра (op 128, фильтр chat_id + sender);")
    print("  * исходящие уходят с человеческими паузами 2–6 c (op 64);")
    print("  * остановка (DISCONNECT ядра) закрывает ws и потоки без зависаний.")
    print()
    print("Живую отправку вне CryptoLayer можно проверить отдельно:")
    print("    python scripts/smoke.py --token ... --device-id ... --chat-id ...")
    print('        --send-test "привет"')


# --- live send test (contract §2 / §6 / §9) ----------------------------------


async def safe_disconnect(client) -> None:
    """Idempotent-ish disconnect wrapper (contract §6: disconnect is NOT idempotent)."""
    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 - contract says generic exceptions here
        print(f"[warn] disconnect raised (ignored): {type(exc).__name__}: {exc}",
              file=sys.stderr)


async def run_send_test(token: str, device_id: str, chat_id: int, text: str) -> int:
    from vkmax.client import MaxClient  # lazy import per contract
    from vkmax.functions.messages import send_message

    client = MaxClient()
    try:
        await client.connect()  # contract §2 step 1
    except Exception as exc:
        print(f"[error] connect failed ({mask(token)}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print("[hint] проверьте сеть/доступность ws-api.oneme.ru", file=sys.stderr)
        await safe_disconnect(client)
        return 1

    try:
        await asyncio.wait_for(
            client.login_by_token(token, device_id), timeout=30
        )  # contract §2 step 2
    except Exception as exc:
        # Contract §8: invalid token raises generic Exception with server text.
        print(
            f"[error] login failed for token {mask(token)} / device {mask(device_id)}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print("[hint] проверьте Token (__oneme_auth) и Device ID (__oneme_device_id) "
              "из LocalStorage web.max.ru", file=sys.stderr)
        await safe_disconnect(client)
        return 1

    try:
        resp = await asyncio.wait_for(
            send_message(client, chat_id, text), timeout=30
        )  # contract §9: send_message(client, chat_id, text), op 64
        print(f"[ok] message sent to chat {chat_id} (response: {resp!r})")
    except Exception as exc:
        print(
            f"[error] send failed to chat {chat_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print("[hint] проверьте Chat ID (терминальные ошибки auth/permission означают "
              "невалидный токен или недоступный диалог)", file=sys.stderr)
        await safe_disconnect(client)
        return 1

    await safe_disconnect(client)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke.py",
        description=(
            "Doc-canonical smoke checklist for the MAX module "
            "(+ optional live send test outside CryptoLayer)."
        ),
    )
    parser.add_argument("--token",
                        help="__oneme_auth из LocalStorage web.max.ru, маскируется")
    parser.add_argument("--device-id",
                        help="__oneme_device_id из web.max.ru, маскируется")
    parser.add_argument("--chat-id", type=int,
                        help="числовой id диалога (см. scripts/discover_chats.py)")
    parser.add_argument("--send-test", metavar="ТЕКСТ",
                        help="отправить ТЕКСТ в диалог --chat-id"
                             " (требует все три аргумента)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.send_test is not None:
        missing = [
            name
            for name, val in (("--token", args.token),
                              ("--device-id", args.device_id),
                              ("--chat-id", args.chat_id))
            if not val
        ]
        if missing:
            print("[error] --send-test requires all of: --token --device-id --chat-id; "
                  f"missing: {', '.join(missing)}", file=sys.stderr)
            parser.print_usage(sys.stderr)
            return 2
        return asyncio.run(run_send_test(args.token, args.device_id,
                                         int(args.chat_id), args.send_test))

    print_checklist()
    if args.token or args.device_id or args.chat_id:
        print()
        print("(credentials given without --send-test — checklist printed only; "
              f"token={mask(args.token)}, device={mask(args.device_id)}, "
              f"chat_id={args.chat_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
