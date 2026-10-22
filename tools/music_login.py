"""Вход в Яндекс Музыку (OAuth Device Flow) — токен для включения музыки на станции.

    .venv/Scripts/python tools/music_login.py

Скрипт покажет адрес и код: открой адрес в браузере, войди в свой аккаунт Яндекса (с Плюсом) и введи код.
Пароль скрипт не видит. Токен сохраняется в data/music_token.json, сервер подхватит его без перезапуска
при следующей музыкальной команде (или перезапусти сервер).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from alice_server.config import load_config  # noqa: E402


def main() -> None:
    from yandex_music import Client  # type: ignore

    out = load_config().data_path / "music_token.json"

    def on_code(code) -> None:
        print("\n1) Открой в браузере:", code.verification_url)
        print("2) Введи код:        ", code.user_code)
        print("\nЖду подтверждения...", flush=True)

    token = Client().device_auth(on_code=on_code)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"access_token": token.access_token, "refresh_token": token.refresh_token,
                               "expires_in": token.expires_in}, ensure_ascii=False), encoding="utf-8")
    me = Client(token.access_token).init().me
    plus = getattr(me, "plus", None)
    print(f"\nГотово: вошли как {me.account.display_name or me.account.login}, "
          f"Плюс: {'есть' if plus and plus.has_plus else 'нет'}. Токен: {out}")


if __name__ == "__main__":
    main()
