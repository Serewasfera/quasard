"""Эмуляция uniproxy: WebSocket /uni.ws (JSON-протокол SpeechKit, как в прошивке 1.56).

Сценарий голосового запроса (по HAR):
  -> System.SynchronizeState            <- System.SynchronizeStateResponse {SessionId}, System.Push(notify)
  -> System.Ack
  -> Vins.VoiceInput (streamId=1)       <- System.InputStartAck
  -> бинарные фреймы [streamId][Ogg/Opus 16k]
  -> streamcontrol action=2 (конец фразы-активации), не закрывает поток
                                        <- ASR.Result (партиалы), ASR.Result endOfUtt=true
                                        <- streamcontrol close stream 1
  -> streamcontrol close stream 1       <- Spotter.Validation, Vins.VinsResponse,
                                           TTS.Speak (streamId=чётный), [streamId][Ogg/Opus 48k], streamcontrol close
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from . import aliceapi, intents, music, protocol as P
from .asr import EndpointDetector, Recognizer
from .audio import OggOpusStreamEncoder, OpusStreamDecoder, frame_stream, parse_stream_frame, split_ogg_pages
from .config import Config
from .llm import Dialog
from .music import Music
from .tts import Synthesizer
from .ytmusic import YTMusicAsync

log = logging.getLogger("uniproxy")

# streamcontrol.action (по прошивке: Close=0, SpotterEnd=2, LazyTTS=3 с полем size)
STREAM_CLOSE = 0
STREAM_SPOTTER_END = 2
# streamcontrol.reason для Close: OK=0, INTERNAL_ERROR=2, STOP_RECORDING=5, AUDIO_SOURCE_STOPPED=6,
# CANCEL=7, NO_SPEECH=8
REASON_CANCEL = 7
REASON_NO_SPEECH = 8


@dataclass
class VoiceRequest:
    ref: str  # messageId события VoiceInput
    stream_id: int
    request_id: str
    sequence_number: int
    spotter_validation: bool
    decoder: OpusStreamDecoder
    detector: EndpointDetector
    started: float = field(default_factory=time.monotonic)
    finalized: bool = False
    partial_no: int = 0


class Services:
    """Общие для всех соединений тяжёлые объекты."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.recognizer = Recognizer(cfg.asr)
        self.dialog = Dialog(cfg.llm, cfg.device.timezone)
        self.tts = Synthesizer(cfg.tts, cfg.data_path)
        self.music = Music(cfg.music, cfg.data_path)
        auth = cfg.resolve(cfg.ytmusic.auth) if cfg.ytmusic.auth else cfg.data_path / "ytmusic_browser.json"
        self.ytmusic = YTMusicAsync(cfg.ytmusic, auth)
        self.connections: dict[str, set["UniproxySession"]] = {}

    def music_source(self, device: str) -> str | None:
        """Какой проигрыватель сейчас ведёт устройства: 'yandex' | 'youtube' | None.

        Источник не меняется на «дальше/лайк» — сменить его можно только новой командой включения.
        """
        if device in self.music.sessions:
            return "yandex"
        if device in self.ytmusic.sessions:
            return "youtube"
        return None

    def pick_source(self, action: str, query: str) -> str:
        """Куда отправить команду wave/search по configured source."""
        want = (self.cfg.music.source or "yandex").lower()
        ym_ok = self.music.enabled
        yt_ok = self.ytmusic.enabled
        if want == "youtube":
            return "youtube" if yt_ok else ("yandex" if ym_ok else "none")
        if want == "auto":
            # «в ютубе/ютуб музыка» в запросе — явный сигнал уйти в YouTube
            q = query.lower()
            if yt_ok and any(w in q for w in ("ютуб", "youtube", "йутуб", "иутуб")):
                return "youtube"
            if action == "wave" and ym_ok:
                return "yandex"  # «просто музыку» — куда надёжнее
            return "yandex" if ym_ok else ("youtube" if yt_ok else "none")
        return "yandex" if ym_ok else ("youtube" if yt_ok else "none")


class UniproxySession:
    def __init__(self, ws: WebSocket, svc: Services) -> None:
        self.ws = ws
        self.svc = svc
        self.cfg = svc.cfg
        h = ws.headers
        self.device_id = h.get("x-uprx-device-id", "unknown")
        self.session_id = h.get("x-uprx-ssid") or str(uuid.uuid4())
        self.uuid = h.get("x-uprx-uuid", "")
        self.state: dict = {}
        self.proto = False  # станция прислала AAPI-кадр -> отвечаем protobuf-ом (прошивки 3.x)
        self._send_lock = asyncio.Lock()
        self._next_out_stream = 2  # серверные потоки — чётные
        self._voice: dict[int, VoiceRequest] = {}
        self._tasks: set[asyncio.Task] = set()
        self._current: asyncio.Task | None = None
        self._traffic = None
        if self.cfg.server.log_traffic:
            d = self.cfg.data_path / "traffic"
            d.mkdir(parents=True, exist_ok=True)
            self._traffic = open(d / f"{time.strftime('%Y%m%d')}_{self.device_id}.jsonl", "a", encoding="utf-8")

    # ------------------------------------------------------------ io

    def _log_traffic(self, direction: str, msg) -> None:
        if self._traffic is None:
            return
        rec = {"t": time.time(), "dir": direction, "ssid": self.session_id}
        rec["msg" if isinstance(msg, dict) else "bin"] = msg if isinstance(msg, dict) else len(msg)
        self._traffic.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._traffic.flush()

    async def send_json(self, msg: dict) -> None:
        """Отправка сообщения сервера. Внутри всё в JSON-формате 1.56; для прошивок 3.x — в proto."""
        self._log_traffic("out", msg)
        if self.proto:
            pm = aliceapi.server_to_proto(msg)
            if pm is None:
                return
            data = aliceapi.encode_server(pm)
            async with self._send_lock:
                await self.ws.send_bytes(data)
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(data)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        t.add_done_callback(_log_task_error)
        return t

    # ------------------------------------------------------------ main loop

    async def run(self) -> None:
        log.info("WS подключение: device=%s ssid=%s", self.device_id, self.session_id)
        self.svc.connections.setdefault(self.device_id, set()).add(self)
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await self._on_text(msg["text"])
                elif msg.get("bytes") is not None:
                    await self._on_binary(msg["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            for t in list(self._tasks):
                t.cancel()
            self.svc.connections.get(self.device_id, set()).discard(self)
            if self._traffic:
                self._traffic.close()
            log.info("WS отключение: device=%s ssid=%s", self.device_id, self.session_id)

    async def _on_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Не JSON: %.200s", text)
            return
        await self._on_message(msg)

    async def _on_message(self, msg: dict) -> None:
        self._log_traffic("in", msg)
        if "streamcontrol" in msg:
            await self._on_stream_control(msg["streamcontrol"])
            return
        event = msg.get("event")
        if not event:
            log.info("Неизвестное сообщение: %.300s", json.dumps(msg, ensure_ascii=False))
            return
        header = event.get("header", {})
        name = f"{header.get('namespace')}.{header.get('name')}"
        payload = event.get("payload", {})
        req_event = (payload.get("request") or {}).get("event") or {}
        if header.get("namespace") == "Vins" and req_event.get("type") == "server_action":
            # колбэки плеера и @@mm_stack_engine_get_next — станция возвращает нам server_action-директивы
            await self._on_server_action(header, payload, req_event)
            return
        handler = {
            "System.SynchronizeState": self._on_sync_state,
            "System.Ack": None,
            "System.Ping": None,
            "System.EchoRequest": self._on_echo,
            "Vins.CancelRequest": self._on_cancel,
            "Log.Spotter": self._on_log_spotter,
            "Vins.VoiceInput": self._on_voice_input,
            "ASR.Recognize": self._on_voice_input,
            "Vins.TextInput": self._on_text_input,
            "Vins.VinsRequest": self._on_text_input,
            "Vins.CustomInput": self._on_text_input,
            "TTS.Generate": self._on_tts_generate,
        }.get(name, "unknown")
        if handler is None:
            return
        if handler == "unknown":
            if header.get("name") == "RequestStat":
                # без подтверждения станция повторяет RequestStat
                await self.send_json(P.directive("Log", "RequestStatAck", {}, ref=header.get("messageId")))
                return
            if name.startswith("Log.") or name.startswith("Spotter."):
                return
            log.info("Необработанное событие %s: %.500s", name, json.dumps(payload, ensure_ascii=False))
            if header.get("namespace") == "Vins":
                await self._respond_empty(header.get("messageId", ""), payload)
            return
        await handler(header, payload)

    async def _on_binary(self, data: bytes) -> None:
        if len(data) < 4:
            return
        if data.startswith(aliceapi.MAGIC):
            # прошивка 3.x: события идут protobuf-ом — переключаем сессию и переводим в JSON-вид
            if not self.proto:
                log.info("Станция говорит protobuf-протоколом (AAPI)")
                self.proto = True
            try:
                msg = aliceapi.client_to_json(aliceapi.decode_client(data))
            except Exception:
                log.exception("Не удалось разобрать AAPI-сообщение")
                return
            if msg is not None:
                await self._on_message(msg)
            return
        stream_id, chunk = parse_stream_frame(data)
        req = self._voice.get(stream_id)
        if req is None or req.finalized:
            return
        try:
            req.detector.feed(req.decoder.feed(chunk))
        except Exception:
            log.exception("Ошибка обработки аудио")
            return
        if req.detector.is_end():
            await self._finalize_voice(req, reason="vad")

    # ------------------------------------------------------------ System

    async def _on_sync_state(self, header: dict, payload: dict) -> None:
        self.state = payload
        ref = header.get("messageId", "")
        await self.send_json(P.sync_state_response(ref, self.session_id))
        await self.send_json(P.push_notify(ref))

    async def _on_log_spotter(self, header: dict, payload: dict) -> None:
        # станция присылает запись фразы активации (отдельный поток PCM) и ждёт подтверждения
        await self.send_json(P.directive("Log", "Ack", {}, ref=header.get("messageId")))

    async def _on_cancel(self, header: dict, payload: dict) -> None:
        if self._current and not self._current.done():
            self._current.cancel()
        for req in self._voice.values():
            req.finalized = True

    async def _on_echo(self, header: dict, payload: dict) -> None:
        await self.send_json(P.directive("System", "EchoResponse", payload, ref=header.get("messageId")))

    # ------------------------------------------------------------ голос

    async def _on_voice_input(self, header: dict, payload: dict) -> None:
        ref = header["messageId"]
        stream_id = int(header.get("streamId", 1))
        hdr = payload.get("header") or {}
        req = VoiceRequest(
            ref=ref,
            stream_id=stream_id,
            request_id=hdr.get("request_id") or str(uuid.uuid4()),
            sequence_number=int(hdr.get("sequence_number") or 0),
            spotter_validation=bool(payload.get("enable_spotter_validation")),
            decoder=OpusStreamDecoder(),
            detector=EndpointDetector(self.cfg.asr),
        )
        # новый запрос отменяет текущий ответ
        if self._current and not self._current.done():
            self._current.cancel()
        for old in self._voice.values():
            old.finalized = True
        self._voice = {stream_id: req}
        await self.send_json(P.input_start_ack(ref))
        self._spawn(self._partials_loop(req))

    async def _partials_loop(self, req: VoiceRequest) -> None:
        """Пустые промежуточные ASR.Result + страховка по времени (как делает uniproxy)."""
        interval = self.cfg.asr.partial_interval_ms / 1000
        hard_limit = self.cfg.asr.max_utterance_ms / 1000 + 3
        while not req.finalized:
            await asyncio.sleep(interval)
            if req.finalized:
                return
            if time.monotonic() - req.started > hard_limit:
                await self._finalize_voice(req, reason="timeout")
                return
            await self.send_json(P.asr_result(req.ref, "", req.partial_no, False, req.detector.duration_ms))
            req.partial_no += 1

    async def _on_stream_control(self, sc: dict) -> None:
        stream_id = sc.get("streamId")
        req = self._voice.get(stream_id)
        if req is None:
            return
        action = sc.get("action")
        if action == STREAM_SPOTTER_END and not req.finalized:
            req.detector.mark_spotter_end()
        elif action == STREAM_CLOSE:
            reason = sc.get("reason")
            if not req.finalized:
                if reason == REASON_CANCEL:
                    # станция отменила запрос (нажали кнопку, перебили) — не отвечаем
                    req.finalized = True
                    log.info("Запрос отменён станцией")
                elif reason == REASON_NO_SPEECH:
                    req.detector.speech_started = False
                    await self._finalize_voice(req, reason="client_no_speech")
                else:
                    # станция сама закончила запись (STOP_RECORDING и т.п.) — распознаём то, что есть
                    await self._finalize_voice(req, reason=f"client_close:{reason}")
            self._voice.pop(stream_id, None)

    async def _finalize_voice(self, req: VoiceRequest, reason: str) -> None:
        if req.finalized:
            return
        req.finalized = True
        log.info("Конец фразы (%s), %.1f c аудио", reason, req.decoder.pcm.size / 16000)
        self._current = self._spawn(self._process_voice(req))

    async def _process_voice(self, req: VoiceRequest) -> None:
        pcm = req.detector.speech_only(req.decoder.pcm)  # VAD: выкидываем тишину
        try:
            text = await self.svc.recognizer.transcribe(pcm) if req.detector.speech_started else ""
        except Exception:
            log.exception("Ошибка ASR")
            text = ""
        text = _strip_wake_word(text)
        log.info("ASR: %r", text)
        await self.send_json(P.asr_result(req.ref, text, req.partial_no, True, req.detector.duration_ms))
        await self.send_json(P.stream_control(req.stream_id))
        if req.spotter_validation:
            await self.send_json(P.spotter_validation(req.ref, self.device_id, True))
        await self._answer(req.ref, req.request_id, req.sequence_number, text)

    # ------------------------------------------------------------ текст

    async def _on_text_input(self, header: dict, payload: dict) -> None:
        ref = header.get("messageId", "")
        request = payload.get("request") or {}
        event = request.get("event") or {}
        hdr = payload.get("header") or {}
        text = event.get("text") or payload.get("text") or ""
        if event.get("type") not in (None, "text_input") or not text:
            log.info("Vins-событие без текста (%s %s) — пустой ответ", event.get("type"), event.get("name"))
            await self._respond_empty(ref, payload)
            return
        if self._current and not self._current.done():
            self._current.cancel()
        self._current = self._spawn(self._answer(ref, hdr.get("request_id") or str(uuid.uuid4()),
                                                 int(hdr.get("sequence_number") or 0), text))

    # ------------------------------------------------------------ музыка

    async def _music_command(self, request_id: str, action: str, query: str) -> tuple[str, list[dict]]:
        """Голосовая музыкальная команда -> (что сказать, директивы)."""
        m = self.svc.music
        if action == "pause":
            return "", [P.client_action("player_pause", {"smooth": True})]
        if action == "resume":
            return "", [P.client_action("player_continue")]
        if not m.enabled:
            return "Музыка не подключена. Запусти на сервере tools/music_login.py.", []
        try:
            if action == "what":
                s = m.sessions.get(self.device_id)
                return (music.say_track(s.current) if s and s.current else "Сейчас ничего не играет."), []
            if action == "like":
                return ("Поставила лайк." if await m.rate(self.device_id, True) else "Сейчас ничего не играет."), []
            if action == "dislike":
                # как у Яндекса: дизлайк и сразу следующий трек
                if await m.rate(self.device_id, False) is None:
                    return "Сейчас ничего не играет.", []
                action = "next"
            if action in ("next", "prev"):
                if self.device_id not in m.sessions:
                    await m.start_wave(self.device_id)
                track = await m.step(self.device_id, 1 if action == "next" else -1, skipped=action == "next")
                speech = ""
            else:
                s = await (m.start_wave(self.device_id) if action == "wave" else m.start_search(self.device_id, query))
                if s is None:
                    return f"Не нашла «{query}» в Яндекс Музыке.", []
                track = await m.step(self.device_id, 1)
                speech = "Включаю «Мою волну»." if action == "wave" else f"Включаю: {music.say_track(track)}"
            if track is None:
                return "Треки закончились.", []
            log.info("Музыка: %s — %s (%s)", track.artists, track.title, track.id)
            return speech, await m.play_directives(self.device_id, request_id, track)
        except Exception:
            log.exception("Ошибка Яндекс Музыки")
            return "Не получилось включить музыку.", []

    async def _on_server_action(self, header: dict, payload: dict, event: dict) -> None:
        ref = header.get("messageId", "")
        hdr = payload.get("header") or {}
        request_id = hdr.get("request_id") or str(uuid.uuid4())
        seq = int(hdr.get("sequence_number") or 0)
        name = event.get("name") or ""
        log.info("server_action от станции: %s", name)
        directives: list[dict] = []
        m = self.svc.music
        try:
            if name == music.CB_PREFIX + "on_failed":
                s = m.sessions.get(self.device_id)
                if s is not None:
                    s.fail_streak += 1
                    log.warning("Станция не смогла проиграть трек %s (подряд: %d)",
                                (event.get("payload") or {}).get("track_id"), s.fail_streak)
                    if s.fail_streak >= 3:  # не «листаем пулемётом» — останавливаемся и говорим
                        m.sessions.pop(self.device_id, None)
                        await self._speak_sentences(ref, request_id, seq,
                                                    _aiter(["Музыка не грузится, станция не может скачать трек."]),
                                                    [], listen_on_question=False)
                        return
            if name == music.CB_PREFIX + "on_started":
                await m.track_started(self.device_id)
            elif name in (music.GET_NEXT, "music_thin_client_recovery", music.CB_PREFIX + "on_failed") and m.enabled:
                if self.device_id in m.sessions:
                    track = await m.step(self.device_id, 1)
                    if track is not None:
                        log.info("Музыка (дальше): %s — %s", track.artists, track.title)
                        directives = await m.play_directives(self.device_id, request_id, track)
        except Exception:
            log.exception("Ошибка обработки server_action %s", name)
        await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, "", speech="",
                                             directives=directives))

    async def _respond_empty(self, ref: str, payload: dict) -> None:
        hdr = payload.get("header") or {}
        await self.send_json(P.vins_response(ref, hdr.get("request_id") or str(uuid.uuid4()), self.session_id,
                                             int(hdr.get("sequence_number") or 0), "", speech=""))

    # ------------------------------------------------------------ ответ

    async def _answer(self, ref: str, request_id: str, seq: int, text: str) -> None:
        if not text.strip():
            await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, "", speech=""))
            return
        intent = intents.match(text)
        if intent is not None:
            if intent.reset_dialog:
                self.svc.dialog.reset(self.device_id)
            await self._speak_sentences(ref, request_id, seq, _aiter([intent.text] if intent.text else []),
                                        intent.directives, listen_on_question=False)
            return
        music_cmd = intents.match_music(text)
        if music_cmd and music_cmd[0] in ("like", "dislike") and self.device_id not in self.svc.music.sessions:
            music_cmd = None  # «мне нравится» без играющей музыки — обычный разговор
        if music_cmd is not None:
            speech, directives = await self._music_command(request_id, *music_cmd)
            await self._speak_sentences(ref, request_id, seq, _aiter([speech] if speech else []), directives,
                                        listen_on_question=False)
            return

        async def llm_sentences():
            try:
                async for s in self.svc.dialog.stream(self.device_id, text):
                    yield s
            except Exception:
                log.exception("Ошибка LLM")
                yield "Извините, мой мозг сейчас недоступен."

        await self._speak_sentences(ref, request_id, seq, llm_sentences(), [],
                                    listen_on_question=self.cfg.llm.listen_after_question)

    async def _speak_sentences(self, ref: str, request_id: str, seq: int, sentences, directives: list[dict],
                               listen_on_question: bool) -> None:
        """Конвейер: фразы (из LLM) -> TTS -> один непрерывный Ogg/Opus поток к станции.

        Vins.VinsResponse уходит, когда готов звук первой фразы; если LLM к этому моменту
        ещё пишет, ждём её не дольше hold_response_ms, чтобы в ответе был полный текст
        и правильный should_listen."""
        queue: asyncio.Queue = asyncio.Queue()
        collected: list[str] = []
        llm_done = asyncio.Event()

        async def producer():
            try:
                async for s in sentences:
                    collected.append(s)
                    await queue.put(s)
            finally:
                llm_done.set()
                await queue.put(None)

        prod = self._spawn(producer())
        enc = None
        sid = 0
        try:
            while (s := await queue.get()) is not None:
                try:
                    pcm, rate = await self.svc.tts.synth_pcm(s)
                except Exception:
                    log.exception("Ошибка TTS")
                    continue
                if not pcm.size:
                    continue
                if enc is None:
                    try:
                        await asyncio.wait_for(llm_done.wait(), self.cfg.llm.hold_response_ms / 1000)
                    except asyncio.TimeoutError:
                        pass
                    text = " ".join(collected)
                    listen = listen_on_question and llm_done.is_set() and text.rstrip().endswith("?")
                    await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, text, speech=text,
                                                         directives=directives, should_listen=listen))
                    sid = self._next_out_stream
                    self._next_out_stream += 2
                    await self.send_json(P.tts_speak(ref, sid))
                    enc = OggOpusStreamEncoder()
                else:
                    await self._send_audio(sid, enc.silence(0.12))
                await self._send_audio(sid, enc.add(pcm, rate))
        finally:
            prod.cancel()
        if enc is None:  # нечего озвучивать (например, команда громкости)
            await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, " ".join(collected),
                                                 speech="", directives=directives))
            return
        await self._send_audio(sid, enc.close())
        await self.send_json(P.stream_control(sid))

    async def _send_audio(self, sid: int, data: bytes) -> None:
        for chunk in split_ogg_pages(data):
            await self.send_bytes(frame_stream(sid, chunk))

    async def _on_tts_generate(self, header: dict, payload: dict) -> None:
        ogg = await self.svc.tts.synth(payload.get("text") or "")
        await self.stream_tts(header.get("messageId", ""), ogg)

    async def stream_tts(self, ref: str, ogg: bytes) -> None:
        sid = self._next_out_stream
        self._next_out_stream += 2
        await self.send_json(P.tts_speak(ref, sid))
        await self._send_audio(sid, ogg)
        await self.send_json(P.stream_control(sid))


async def _aiter(items):
    for x in items:
        yield x


_WAKE_RE = re.compile(r"^\s*(слушай,?\s+)?(алиса|алис|олиса|малиса|яндекс)[\s,.!?]*", re.I)


def _strip_wake_word(text: str) -> str:
    """Яндекс не включает фразу активации в результат ASR — убираем, если ASR её услышал."""
    rest = _WAKE_RE.sub("", text, count=1)
    return rest[:1].upper() + rest[1:] if rest else ""


def _log_task_error(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("Ошибка в задаче", exc_info=t.exception())
