"""Имитатор станции: шлёт на сервер голосовой запрос так же, как Яндекс Станция 2 (по HAR).

    python tools/station_sim.py data/samples/voice.ogg [--url ws://127.0.0.1:8080/uni.ws]
    python tools/station_sim.py --text "Сколько будет два плюс два?"

Ответ TTS сохраняется в data/sim_answer_<n>.ogg.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import struct
import sys
import time
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from alice_server import aliceapi as A  # noqa: E402
DEVICE_ID = "SIMSTATION0001"


def ev(namespace: str, name: str, payload: dict, **hdr) -> tuple[str, str]:
    mid = str(uuid.uuid4())
    return mid, json.dumps({"event": {"header": {"messageId": mid, "name": name, "namespace": namespace, **hdr},
                                      "payload": payload}}, ensure_ascii=False)


def ogg_pages(data: bytes) -> list[bytes]:
    pages, pos = [], 0
    while pos + 27 <= len(data):
        n = data[pos + 26]
        total = 27 + n + sum(data[pos + 27:pos + 27 + n])
        pages.append(data[pos:pos + total])
        pos += total
    return pages


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ogg", nargs="?")
    ap.add_argument("--text")
    ap.add_argument("--url", default="ws://127.0.0.1:80/uni.ws")
    ap.add_argument("--realtime", action="store_true", help="слать аудио в реальном темпе")
    ap.add_argument("--tail-silence", type=float, default=2.0, help="сек. тишины (повтор последней страницы) после фразы")
    ap.add_argument("--proto", action="store_true", help="говорить protobuf-протоколом (AAPI), как прошивки 3.x")
    args = ap.parse_args()

    def sc_msg(action: int, reason: int = 0):
        if args.proto:
            body = {"SpotterEnd": {}} if action == 2 else {"Close": {"Reason": {0: "OK", 7: "CANCEL"}[reason]}}
            return A.encode_client({"StreamControl": {"StreamId": 1, "MessageId": str(uuid.uuid4()), **body}})
        return json.dumps({"streamcontrol": {"action": action, "messageId": str(uuid.uuid4()), "reason": reason,
                                             "streamId": 1}})

    headers = {"X-UPRX-DEVICE-ID": DEVICE_ID, "X-UPRX-SSID": str(uuid.uuid4()), "X-UPRX-UUID": uuid.uuid4().hex,
               "X-UPRX-AUTH-TOKEN": "sim"}
    sslctx = ssl._create_unverified_context() if args.url.startswith("wss") else None
    async with websockets.connect(args.url, additional_headers=headers, ssl=sslctx, max_size=None) as ws:
        req_id = str(uuid.uuid4())
        req_hdr = {"request_id": req_id, "sequence_number": 0, "prev_req_id": None}
        if args.proto:
            mid = lambda: str(uuid.uuid4())  # noqa: E731
            await ws.send(A.encode_client({"Event": {"Header": {"MessageId": mid()}, "SynchronizeStateRequest": {}}}))
            rq = {"Header": {"RequestId": req_id, "SequenceNumber": 0}, "EnableSpotterValidation": True}
            if args.text:
                rq["Request"] = {"Event": {"Type": "text_input", "Text": args.text}}
                await ws.send(A.encode_client({"Event": {"Header": {"MessageId": mid()}, "TextInput": rq}}))
            else:
                rq["Request"] = {"Event": {"Type": "voice_input"}}
                await ws.send(A.encode_client({"Event": {"Header": {"MessageId": mid(), "StreamId": 1}, "VoiceInput": rq}}))
        else:
            _, msg = ev("System", "SynchronizeState", {"auth_token": "sim", "lang": "ru-RU", "uuid": headers["X-UPRX-UUID"]})
            await ws.send(msg)
        t0 = time.monotonic()
        if args.text and not args.proto:
            await ws.send(ev("Vins", "TextInput", {"header": req_hdr, "request": {"event": {"type": "text_input",
                                                                                        "text": args.text}}})[1])
        elif not args.text:
            data = Path(args.ogg).read_bytes()
            if not args.proto:
                _, msg = ev("Vins", "VoiceInput", {"header": req_hdr, "format": "audio/opus", "lang": "ru-RU",
                                                   "enable_spotter_validation": True,
                                                   "request": {"event": {"type": "voice_input"}}}, streamId=1)
                await ws.send(msg)
            pages = ogg_pages(data)
            # Станция шлёт два Ogg-потока: буфер с фразой активации, затем streamcontrol action=2,
            # затем новый поток (BOS-страница с OpusHead) с командой.
            bos = [i for i, p in enumerate(pages) if p[5] & 0x02]
            spotter_end = bos[1] if len(bos) > 1 else 1
            # «тишина» после фразы: повторяем последнюю страницу (сервер ждёт конца фразы по VAD)
            silence = [pages[-1]] * int(args.tail_silence / 0.12)

            async def sender():
                for i, p in enumerate(pages + silence):
                    if i == spotter_end:
                        await ws.send(sc_msg(2))
                    try:
                        await ws.send(struct.pack(">I", 1) + p)
                    except websockets.ConnectionClosed:
                        return
                    if (args.realtime and i > spotter_end) or i >= len(pages):
                        await asyncio.sleep(0.12)
            sender_task = asyncio.create_task(sender())

        audio: dict[int, bytes] = {}
        n = 0
        async for m in ws:
            dt = time.monotonic() - t0
            if isinstance(m, bytes) and m.startswith(A.MAGIC):
                j = proto_to_json(A.decode_server(m))
            elif isinstance(m, bytes):
                sid = struct.unpack(">I", m[:4])[0]
                audio[sid] = audio.get(sid, b"") + m[4:]
                print(f"{dt:6.2f}s <- BIN stream={sid} {len(m) - 4}B")
                continue
            else:
                j = json.loads(m)
            if "streamcontrol" in j:
                sc = j["streamcontrol"]
                print(f"{dt:6.2f}s <- streamcontrol {sc}")
                if sc["streamId"] == 1 and not args.text:
                    sender_task.cancel()
                    await ws.send(sc_msg(0, 7))
                if sc["streamId"] in audio:
                    out = ROOT / "data" / f"sim_answer_{n}.ogg"
                    out.write_bytes(audio.pop(sc["streamId"]))
                    print(f"         TTS сохранён: {out}")
                    n += 1
                    break
                continue
            d = j.get("directive", {})
            h, p = d.get("header", {}), d.get("payload", {})
            name = f"{h.get('namespace')}.{h.get('name')}"
            if name == "ASR.Result":
                print(f"{dt:6.2f}s <- ASR.Result endOfUtt={p['endOfUtt']} text={p['recognition'][0]['normalized']!r}")
            elif name == "Vins.VinsResponse":
                vr = p.get("voice_response", {})
                print(f"{dt:6.2f}s <- VinsResponse speech={vr.get('output_speech', {}).get('text')!r} "
                      f"directives={[x['name'] for x in p['response']['directives']]} listen={vr.get('should_listen')}")
                if not vr.get("has_voice_response"):
                    break
            else:
                print(f"{dt:6.2f}s <- {name} {json.dumps(p, ensure_ascii=False)[:150]}")


def proto_to_json(m: dict) -> dict:
    """Ответ сервера в protobuf -> тот же JSON-вид, что печатаем для прошивки 1.56."""
    if "StreamControl" in m:
        return {"streamcontrol": {"streamId": int(m["StreamControl"]["StreamId"]), "action": 0, "proto": True}}
    ev = m.get("Event", {})
    kind = next(k for k in ev if k != "Header")
    p = ev[kind]
    if kind == "AsrResult":
        rec = p.get("Recognition") or [{}]
        return {"directive": {"header": {"namespace": "ASR", "name": "Result"},
                              "payload": {"endOfUtt": p.get("EndOfUtt", False),
                                          "recognition": [{"normalized": rec[0].get("Normalized", "")}]}}}
    if kind == "AliceResponse":
        vr = p.get("VoiceResponse", {})
        return {"directive": {"header": {"namespace": "Vins", "name": "VinsResponse"}, "payload": {
            "voice_response": {"has_voice_response": vr.get("HasVoiceResponse", False),
                               "should_listen": vr.get("ShouldListen", False),
                               "output_speech": {"text": vr.get("OutputSpeech", {}).get("Text")}},
            "response": {"directives": [{"name": d.get("Name")} for d in p.get("Response", {}).get("Directives", [])]}}}}
    return {"directive": {"header": {"namespace": "proto", "name": kind}, "payload": p}}


if __name__ == "__main__":
    asyncio.run(main())
