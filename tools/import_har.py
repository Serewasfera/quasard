"""Импорт реальных ответов Яндекса из HAR в data/captured (ими сервер отвечает станции).

    python tools/import_har.py capture.har

Берёт последний успешный ответ get_sync_info / device_list / get_user_details,
glagol-сертификаты станций кладёт в data/devices/<id>.json (чтобы они не менялись).
Аудио голосовых запросов сохраняет в data/samples/*.ogg — для tools/station_sim.py.
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alice_server.config import load_config  # noqa: E402
from alice_server.store import Store  # noqa: E402

CAPTURE = {"/get_sync_info": "get_sync_info", "/glagol/device_list": "device_list",
           "/get_user_details": "user_details"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("har")
    args = ap.parse_args()
    cfg = load_config()
    store = Store(cfg)
    entries = json.loads(Path(args.har).read_text(encoding="utf-8"))["log"]["entries"]
    store.captured.mkdir(parents=True, exist_ok=True)
    samples = cfg.data_path / "samples"
    samples.mkdir(parents=True, exist_ok=True)

    for e in entries:
        url = urlparse(e["request"]["url"])
        resp = e["response"]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in CAPTURE and resp["status"] == 200 and not e.get("_mock"):
            data = json.loads(resp["content"]["text"])
            (store.captured / f"{CAPTURE[url.path]}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print("captured", CAPTURE[url.path])
            if url.path == "/get_sync_info" and "glagol" in data:
                store.update_device(q["device_id"], platform=q.get("platform"), glagol=data["glagol"])
            if url.path == "/glagol/device_list":
                for dev in data.get("devices", []):
                    if "glagol" in dev:
                        store.update_device(dev["id"], platform=dev.get("platform"), name=dev.get("name"),
                                            glagol=dev["glagol"])
        if url.path == "/uni.ws":
            up = b""
            for m in e.get("_webSocketMessages", []):
                if m["type"] == "send" and m.get("opcode") == 2 and m.get("data"):
                    raw = base64.b64decode(m["data"])
                    if struct.unpack(">I", raw[:4])[0] == 1:
                        up += raw[4:]
            if up:
                name = samples / f"voice_{e['startedDateTime'][11:19].replace(':', '')}.ogg"
                name.write_bytes(up)
                print("sample", name.name, len(up), "bytes")
    print("Готово:", store.captured)


if __name__ == "__main__":
    main()
