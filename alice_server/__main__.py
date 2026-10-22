"""Запуск: python -m alice_server [--config config.toml]"""
from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from .app import create_app
from .config import load_config


async def _serve(cfg) -> None:
    app = create_app(cfg)
    servers = []
    s = cfg.server
    cert, key = cfg.resolve(s.certfile), cfg.resolve(s.keyfile)
    common = dict(app=app, host=s.host, log_level=s.log_level.lower(), ws_ping_interval=None, ws_max_size=16 * 1024 * 1024,
                  proxy_headers=False)
    if s.https_port:
        if cert.exists() and key.exists():
            servers.append(uvicorn.Server(uvicorn.Config(port=s.https_port, ssl_certfile=str(cert),
                                                         ssl_keyfile=str(key), **common)))
        else:
            logging.warning("HTTPS отключён: нет %s / %s (см. tools/gen_certs.py)", cert, key)
    if s.http_port:
        servers.append(uvicorn.Server(uvicorn.Config(port=s.http_port, **common)))
    if not servers:
        raise SystemExit("Не настроен ни один порт")
    await asyncio.gather(*(srv.serve() for srv in servers))


def main() -> None:
    ap = argparse.ArgumentParser(description="Локальный бэкенд голосового ассистента (GigaAM + LLM + TTS)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    logging.basicConfig(level=cfg.server.log_level, format=fmt)
    # дублируем лог в файл: удобно смотреть удалённо и после закрытия окна
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(cfg.data_path / "server.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).addHandler(fh)
    asyncio.run(_serve(cfg))


if __name__ == "__main__":
    main()
