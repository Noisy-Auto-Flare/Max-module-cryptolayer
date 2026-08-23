#!/usr/bin/env python3
"""dump_profile.py — one-off live diagnostics for docs/vkmax-contract.md §5.

Logs in by token and prints the SHAPE of the login response payload:
top-level keys, the ``profile`` subtree, and the first chat object — so we
can pin down where OUR OWN user id actually lives on the live server
(``payload.profile.id`` was refuted by the live checkpoint).

Secrets are masked: tokens are never printed; any string that looks like a
phone number is reduced to its last 4 digits. Development helper — NOT part
of the Max/ module folder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# --- masking ----------------------------------------------------------------


def _mask_phone_like(value: str) -> str:
    """Reduce phone-like strings (7+ digits, optional +) to ***last4."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) >= 7:
        return f"***{digits[-4:]}"
    return value


def scrub(node):
    """Recursively mask phone-like strings inside dicts/lists."""
    if isinstance(node, dict):
        return {k: scrub(v) for k, v in node.items()}
    if isinstance(node, list):
        return [scrub(v) for v in node]
    if isinstance(node, str):
        return _mask_phone_like(node)
    return node


def mask(secret: str | None) -> str:
    if not secret:
        return "<empty>"
    return f"***{secret[-4:]}"


# --- main -------------------------------------------------------------------


async def run(token: str, device_id: str) -> int:
    from vkmax.client import MaxClient  # lazy import

    client = MaxClient()
    try:
        await client.connect()
    except Exception as exc:
        print(f"[error] connect failed ({mask(token)}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        resp = await asyncio.wait_for(
            client.login_by_token(token, device_id), timeout=30
        )
    except Exception as exc:
        print(f"[error] login failed ({mask(token)}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            await client.disconnect()
        except Exception:
            pass
        return 1

    payload = resp.get("payload") if isinstance(resp, dict) else None
    if not isinstance(payload, dict):
        print("[error] no payload dict in login response", file=sys.stderr)
        return 1

    print("=== top-level payload keys ===")
    print(sorted(payload.keys()))

    print("\n=== payload.profile (masked) ===")
    profile = payload.get("profile")
    print(json.dumps(scrub(profile), ensure_ascii=False, indent=2)
          if profile is not None else "<no profile key>")

    chats = payload.get("chats") or []
    if isinstance(chats, list) and chats:
        print(
            f"\n=== FIRST chat object of payload.chats ({len(chats)} total, masked) ==="
        )
        print(json.dumps(scrub(chats[0]), ensure_ascii=False, indent=2))

    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 — contract §6: raises legally
        print(f"[warn] disconnect raised (ignored): {type(exc).__name__}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dump_profile.py",
        description="Print login-response structure (masked) to pin down own-id path.",
    )
    parser.add_argument("--token", required=True,
                        help="__oneme_auth из LocalStorage web.max.ru")
    parser.add_argument("--device-id", required=True,
                        help="__oneme_device_id из LocalStorage web.max.ru")
    args = parser.parse_args()
    return asyncio.run(run(args.token, args.device_id))


if __name__ == "__main__":
    sys.exit(main())
