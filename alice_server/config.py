"""Конфигурация: config.toml в корне проекта + переопределение через переменные окружения.

Переменная окружения ALICE_<СЕКЦИЯ>_<КЛЮЧ> перекрывает значение из файла,
например ALICE_LLM_BASE_URL=http://127.0.0.1:11434/v1.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    https_port: int = 443
    http_port: int = 80  # 0 — не поднимать HTTP
    certfile: str = "certs/server.crt"
    keyfile: str = "certs/server.key"
    # Адрес uniproxy, который отдаём станции в get_sync_info (system_config.uniProxyUrl).
    # Пусто — оставить оригинальный wss://uniproxy.alice.yandex.net/uni.ws (если DNS уже перенаправлен).
    public_uniproxy_url: str = ""
    data_dir: str = "data"
    log_level: str = "INFO"
    log_traffic: bool = True  # писать JSON-сообщения WS в data/traffic/*.jsonl


@dataclass
class AsrCfg:
    engine: str = "gigaam"  # gigaam | openai
    # gigaam: v3_e2e_rnnt (лучшее качество, с пунктуацией) / v3_e2e_ctc (быстрее) / v3_rnnt (без пунктуации)
    model: str = "v3_e2e_rnnt"
    device: str = "auto"  # auto | cpu | cuda
    language: str = "ru"
    # для engine=openai (любой OpenAI-совместимый /audio/transcriptions)
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "none"
    # VAD / конец фразы
    vad_silence_ms: int = 700  # тишина после речи, после которой фраза считается законченной
    vad_threshold: float = 0.5  # порог Silero VAD (0..1)
    vad_energy_db: float = -68.0  # порог энергетического VAD, если Silero недоступен (сигнал станции тихий)
    max_utterance_ms: int = 10000
    no_speech_timeout_ms: int = 5000  # если речи так и не было
    partial_interval_ms: int = 300


@dataclass
class LlmCfg:
    # Любой OpenAI-совместимый API со стримингом. По умолчанию — бесплатный тариф Groq
    # (ключ: https://console.groq.com/keys). "echo" — отладка без LLM.
    base_url: str = "https://api.groq.com/openai/v1"
    api_key: str = ""
    model: str = "qwen/qwen3.8-27b"
    temperature: float = 0.6
    max_tokens: int = 400
    timeout_s: float = 60.0
    history_turns: int = 8
    history_ttl_s: int = 300
    # Доп. поля запроса (например, выключить «размышления» Qwen). Если сервер их не примет — уберутся сами.
    extra_body: dict = field(default_factory=lambda: {"reasoning_effort": "none"})
    # Продолжать слушать после ответа, если ответ заканчивается вопросом
    listen_after_question: bool = True
    # Сколько ждать окончания ответа LLM после готовности первой озвученной фразы (для полного текста карточки)
    hold_response_ms: int = 400
    # Файл с системным промптом (относительно корня проекта). Если есть — перекрывает system_prompt
    # и перечитывается на лету при изменении, перезапуск не нужен.
    system_prompt_file: str = "system_prompt.txt"
    system_prompt: str = (
        "Ты — голосовой ассистент Алиса в умной колонке. Отвечай по-русски, коротко и по делу "
        "(1–3 предложения), разговорным языком. Никакой разметки, списков, эмодзи и ссылок — "
        "текст будет озвучен синтезатором речи. Числа и единицы пиши так, как их произносят."
    )


@dataclass
class TtsCfg:
    engine: str = "f5"  # f5 | edge | openai
    voice: str = "ru-RU-SvetlanaNeural"  # edge: ru-RU-SvetlanaNeural / ru-RU-DariyaNeural / ru-RU-DmitryNeural
    rate: str = "+0%"  # edge
    # engine=f5: адрес сервиса tools/f5_server.py; voice_id — метка эталона для кэша (сменил эталон — смени метку)
    f5_url: str = "http://127.0.0.1:8777/v1"
    f5_voice_id: str = "ref_voice_1"
    # engine=openai: OpenAI-совместимый /audio/speech
    base_url: str = "http://127.0.0.1:8880/v1"
    api_key: str = "none"
    openai_model: str = "tts-1"
    cache: bool = True


@dataclass
class MusicCfg:
    enabled: bool = True
    token: str = ""  # пусто — берётся из data/music_token.json (tools/music_login.py)
    quality: str = "high"  # high — mp3 320, low — 128/192


@dataclass
class DeviceCfg:
    """Что отвечаем станции в ответах quasar/oauth, если нет захваченных из HAR данных."""
    name: str = "Яндекс Станция"
    uid: int = 100000001
    display_name: str = "Пользователь"
    latitude: float = 55.7558
    longitude: float = 37.6173
    timezone: str = "Europe/Moscow"
    timezone_offset_sec: int = 10800


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    asr: AsrCfg = field(default_factory=AsrCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    music: MusicCfg = field(default_factory=MusicCfg)
    device: DeviceCfg = field(default_factory=DeviceCfg)

    @property
    def data_path(self) -> Path:
        p = Path(self.server.data_dir)
        return p if p.is_absolute() else ROOT / p

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else ROOT / p


def _coerce(value: str, typ):
    if typ is bool or typ == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on")
    if typ is int or typ == "int":
        return int(value)
    if typ is float or typ == "float":
        return float(value)
    if typ is dict or typ == "dict":
        import json

        return json.loads(value)
    return value


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("ALICE_CONFIG", ROOT / "config.toml"))
    raw = {}
    if path.exists():
        # utf-8-sig: Блокнот и PowerShell 5.1 пишут BOM, а tomllib его не принимает
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg = Config()
    for section in fields(cfg):
        obj = getattr(cfg, section.name)
        values = raw.get(section.name, {})
        for f_ in fields(obj):
            if f_.name in values:
                setattr(obj, f_.name, values[f_.name])
            env = os.environ.get(f"ALICE_{section.name.upper()}_{f_.name.upper()}")
            if env is not None:
                setattr(obj, f_.name, _coerce(env, f_.type))
    return cfg
