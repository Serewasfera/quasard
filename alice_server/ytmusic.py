"""Музыка из YouTube Music в формате, который понимает станция.

Как и с Яндекс Музыкой (music.py): очередь треков на станцию, «дальше» через @@mm_stack_engine_get_next,
callbacks on_started/on_finished — только для статистики. Отличия:
  - поиск и радиоволна («включи похожие») идут через ytmusicapi (без авторизации работает поиск);
  - ссылку на аудио выдаёт yt-dlp: он расшифровывает signature-параметры stream-URL'ов YouTube,
    которые напрямую недоступны (в get_song() у ytmusicapi они зашифрованы);
  - формат — AAC/mp4 (itag 140 ~128 kbps), станции он подходит: плеер умеет и mp4-поток, не только mp3;
  - лайк/дизлайк — rate_song под своим аккаунтом (browser.json/OAuth из tools/ytmusic_login.py).

Слоя event loop здесь нет намеренно: ytmusicapi и yt-dlp — синхронные библиотеки, вызываем их
через asyncio.to_thread из uniproxy (см. YTMusicAsync ниже).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from .config import YtMusicCfg
from .music import GET_NEXT, CB_PREFIX, TrackInfo  # общий протокол директив и тип трека

log = logging.getLogger("ytmusic")

# тот же server_action, что и у Яндекса: станция шлёт его, когда трек кончился или сказали «дальше»


@dataclass
class Session:
    kind: str  # "radio" | "list"
    title: str
    seed: str = ""  # videoId, от которого построена волна
    queue: list[TrackInfo] = field(default_factory=list)
    pos: int = -1
    started_at: float = 0.0
    fail_streak: int = 0

    @property
    def current(self) -> TrackInfo | None:
        return self.queue[self.pos] if 0 <= self.pos < len(self.queue) else None


def _thumb(thumbs: list) -> str:
    best = ""
    for t in thumbs or []:
        u = t.get("url") or ""
        if u:
            best = u  # последний — самый большой
    # i.ytimg.com возвращает разную картинку по суффиксу; для карточки годится любой
    return best


def _track_from_row(row: dict) -> TrackInfo | None:
    vid = row.get("videoId") or ""
    if not vid:
        return None
    artists = ", ".join(a.get("name", "") for a in (row.get("artists") or []) if a.get("name"))
    dur = row.get("duration") or row.get("length") or ""
    duration_ms = 0
    if isinstance(dur, (int, float)):
        duration_ms = int(dur * 1000)
    elif isinstance(dur, str):  # "3:56" / "1:02:41"
        parts = [int(x) for x in dur.split(":") if x.isdigit()]
        if parts:
            seconds = 0
            for p in parts:
                seconds = seconds * 60 + p
            duration_ms = seconds * 1000
    return TrackInfo(id=vid, title=row.get("title") or "", artists=artists,
                     duration_ms=duration_ms, cover=_thumb(row.get("thumbnails")))


class YTMusic:
    """Синхронная обёртка над ytmusicapi/yt-dlp. Все методы блокирующие — вызывать из to_thread."""

    def __init__(self, cfg: YtMusicCfg, auth_file) -> None:
        self.cfg = cfg
        self.auth_file = auth_file  # Path к browser.json / oauth token (может отсутствовать)
        self._yt = None
        self.sessions: dict[str, Session] = {}

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _has_auth(self) -> bool:
        return self.auth_file is not None and self.auth_file.exists()

    def client(self):
        """Ленивая инициализация ytmusicapi. Без auth — анонимный клиент (поиск/волна работают)."""
        if self._yt is None:
            from ytmusicapi import YTMusic as _YT  # type: ignore

            auth = str(self.auth_file) if self._has_auth() else None
            try:
                self._yt = _YT(auth=auth, language="ru", location="US")
            except Exception:  # auth-файл повреждён/устарел — падаем на анонимный режим
                log.exception("YouTube Music: не удалось войти по %s, продолжаю анонимно", self.auth_file)
                self._yt = _YT(language="ru", location="US")
            log.info("YouTube Music: клиент готов (авторизация: %s)", "есть" if auth else "нет")
        return self._yt

    # ------------------------------------------------------------ источники
    def start_wave(self, device: str) -> Session:
        """«Моя волна» в стиле YT Music: строим radio-плейлист поверх случайного трека из истории/лайков.

        Без авторизации (и если библиотека пуста) берём первый трек поиска по популярному запросу.
        """
        yt = self.client()
        seed_track = None
        if self._has_auth():  # волна «вокруг того, что ты слушаешь» — с личного seed'а
            for fetch in (lambda: yt.get_history(limit=5).get("records", []),
                          lambda: yt.get_liked_songs(limit=5).get("records", [])):
                try:
                    rows = [r for r in fetch() if r and r.get("videoId")]
                except Exception:  # noqa: BLE001 — без Premium история/лайки могут быть недоступны
                    rows = []
                if rows:
                    seed_track = _track_from_row(rows[0])
                    break
        if seed_track is None:
            try:
                rows = yt.search("top hits", filter="songs", limit=5)
            except Exception:  # noqa: BLE001
                rows = []
            seed_track = _track_from_row(rows[0]) if rows else None
        if seed_track is None:
            raise RuntimeError("YouTube Music: не удалось найти стартовый трек для волны")
        s = Session(kind="radio", title="Волна YouTube Music", seed=seed_track.id, queue=[seed_track])
        self._radio_more(yt, s)
        self.sessions[device] = s
        return s

    def _radio_more(self, yt, s: Session) -> None:
        """Добавляет ещё треков из радиоволны вокруг seed (get_watch_playlist radio=True)."""
        known = {t.id for t in s.queue}
        try:
            wp = yt.get_watch_playlist(videoId=s.seed or None, playlistId=None, limit=25, radio=True)
        except Exception:  # noqa: BLE001
            log.exception("YouTube Music: волна недоступна")
            return
        added = 0
        for row in wp.get("tracks", []):
            t = _track_from_row(row)
            if t and t.id not in known:
                s.queue.append(t)
                known.add(t.id)
                added += 1
        log.info("YouTube Music: волна +%d треков", added)

    def start_search(self, device: str, query: str) -> Session | None:
        yt = self.client()
        try:
            rows = yt.search(query, filter="songs", limit=self.cfg.limit_search)
        except Exception:  # noqa: BLE001
            log.exception("YouTube Music: поиск не удался")
            return None
        tracks = [t for t in (_track_from_row(r) for r in rows) if t]
        if not tracks:
            # «группа X» / «альбом Y»: пробуем лучший результат общего поиска
            try:
                best = yt.search(query, limit=5)
            except Exception:  # noqa: BLE001
                best = []
            for b in best:
                if b.get("resultType") == "album" and b.get("browseId"):
                    album = yt.get_album(b["browseId"])
                    tracks = [t for t in (_track_from_row(x) for x in (album.get("tracks") or [])) if t]
                    if tracks:
                        break
                if b.get("resultType") == "artist" and b.get("channelId"):
                    artist = yt.get_artist(b["channelId"])
                    songs_flow = artist.get("songs") or {}
                    songs = []
                    if songs_flow.get("params"):
                        try:
                            songs = yt.get_artist_song_recommendations(b["channelId"]) \
                                if hasattr(yt, "get_artist_song_recommendations") else []
                        except Exception:  # noqa: BLE001
                            songs = []
                    tracks = [t for t in (_track_from_row(x) for x in songs) if t]
                    if tracks:
                        break
        if not tracks:
            return None
        s = Session(kind="list", title=query, queue=tracks[: max(5, self.cfg.limit_search)])
        self.sessions[device] = s
        return s

    # ------------------------------------------------------------ навигация
    def step(self, device: str, delta: int) -> TrackInfo | None:
        s = self.sessions.get(device)
        if s is None:
            return None
        s.pos = max(0, s.pos + delta)
        if s.kind == "radio" and s.pos >= len(s.queue) - 2:
            self._radio_more(self.client(), s)
        if s.pos >= len(s.queue):
            return None
        return s.current

    # ------------------------------------------------------------ поток
    def stream_url(self, track: TrackInfo) -> tuple[str, int]:
        """Прямая ссылка на аудио через yt-dlp (он расшифровывает signature-параметры YouTube).

        Берём AAC (~128 kbps, itag 140) — станции хватает, m4a-поток её плеер переваривает.
        Ссылки YouTube живут несколько часов — генерируем заново на каждый audio_play.
        """
        import yt_dlp  # type: ignore

        fmt = "bestaudio[acodec^=mp4a]/bestaudio[abr<=70]/bestaudio" \
            if self.cfg.quality == "high" else "bestaudio[abr<=50]/worstaudio"
        opts = {"quiet": True, "noplaylist": True, "skip_download": True, "format": fmt,
                "socket_timeout": 15, "nocheckcertificate": False,
                "extractor_args": {"youtube": {"player_client": ["android", "web"]}}}
        url_page = f"https://music.youtube.com/watch?v={track.id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url_page, download=False)
        chosen = info.get("requested_formats") or [info]
        pick = next((f for f in chosen if f.get("acodec") not in (None, "none")), None)
        if pick is None or not pick.get("url"):
            raise RuntimeError(f"yt-dlp: нет аудиопотока для {track.id}")
        bitrate = int(pick.get("abr") or pick.get("tbr") or 128)
        # иногда duration точнее в метаданных ролика
        if not track.duration_ms and info.get("duration"):
            track.duration_ms = int(info["duration"] * 1000)
        return pick["url"], max(32, bitrate)

    # ------------------------------------------------------------ оценки
    def rate(self, device: str, like: bool) -> TrackInfo | None:
        s = self.sessions.get(device)
        if s is None or s.current is None:
            return None
        if not self._has_auth():
            return s.current  # скажем «лайк», но без аккаунта он не сохранится — см. speech в uniproxy
        try:
            from ytmusicapi import LikeStatus  # type: ignore

            status = LikeStatus.LIKE if like else LikeStatus.DISLIKE
            self.client().rate_song(s.current.id, status)
        except Exception:  # noqa: BLE001
            log.exception("YouTube Music: не удалось поставить оценку")
        return s.current

    def track_started(self, device: str) -> None:
        s = self.sessions.get(device)
        if s is None or s.current is None:
            return
        s.started_at = time.time()
        s.fail_streak = 0
        if self._has_auth():
            try:
                self.client().add_history_item(s.current.id)
            except Exception:  # noqa: BLE001 — обратная связь не критична
                pass

    # ------------------------------------------------------------ директивы для станции
    def play_directives_sync(self, device: str, request_id: str, track: TrackInfo) -> list[dict]:
        """audio_play + @@mm_stack_engine_get_next — те же директивы, что отдаёт Яндекс (см. music.py)."""
        s = self.sessions[device]
        url, bitrate = self.stream_url(track)
        nxt = s.queue[s.pos + 1] if s.pos + 1 < len(s.queue) else None
        music_meta = {"type": "Radio" if s.kind == "radio" else "Playlist",
                      "id": s.seed or s.title, "description": "", "repeat_mode": "None"}
        if nxt:
            music_meta["next_track_info"] = {"type": "Track", "id": nxt.id}

        def cb(name: str, ignore: bool = True) -> dict:
            return {"name": CB_PREFIX + name, "payload": {"track_id": track.id, "@request_id": request_id},
                    "ignore_answer": ignore, "type": "server_action", "is_led_silent": True}

        audio_play = {
            "name": "audio_play",
            "sub_name": "music",
            "type": "client_action",
            "is_led_silent": True,
            "payload": {
                "stream": {"id": track.id, "type": "Track", "url": url, "format": "M4A", "codec": "Aac",
                           "bitrate": bitrate, "offset_ms": 0, "duration_ms": track.duration_ms},
                "metadata": {"title": track.title, "subtitle": track.artists, "art_image_url": track.cover,
                             "hide_progress_bar": False, "glagol_metadata": {"music_metadata": music_meta}},
                "callbacks": {"on_started": cb("on_started"), "on_stopped": cb("on_stopped"),
                              "on_finished": cb("on_finished"), "on_failed": cb("on_failed", ignore=False)},
                "scenario_meta": {"owner": "music"},
                "background_mode": "Ducking",
                "screen_type": "Music",
                "provider_name": "YouTube Music",
                "set_pause": False,
                "playback_speed": 1,
                "karaoke_mode": "Undefined",
            },
        }
        get_next = {
            "name": GET_NEXT,
            "type": "server_action",
            "is_led_silent": True,
            "payload": {"@request_id": request_id, "stack_session_id": request_id,
                        "@scenario_name": "HollywoodMusic", "stack_product_scenario_name": "music",
                        "@recovery_params": {}, "aqw_device": device},
        }
        return [audio_play, get_next]


class YTMusicAsync:
    """Event-loop-обёртка: ytmusicapi/yt-dlp синхронные — уводим вызовы в to_thread."""

    def __init__(self, cfg: YtMusicCfg, auth_file) -> None:
        self._impl = YTMusic(cfg, auth_file)

    @property
    def enabled(self) -> bool:
        return self._impl.enabled

    @property
    def sessions(self) -> dict[str, Session]:
        return self._impl.sessions

    @property
    def has_auth(self) -> bool:
        return self._impl._has_auth()

    async def start_wave(self, device: str) -> Session:
        return await asyncio.to_thread(self._impl.start_wave, device)

    async def start_search(self, device: str, query: str) -> Session | None:
        return await asyncio.to_thread(self._impl.start_search, device, query)

    async def step(self, device: str, delta: int) -> TrackInfo | None:
        return await asyncio.to_thread(self._impl.step, device, delta)

    async def rate(self, device: str, like: bool) -> TrackInfo | None:
        return await asyncio.to_thread(self._impl.rate, device, like)

    async def track_started(self, device: str) -> None:
        await asyncio.to_thread(self._impl.track_started, device)

    async def play_directives(self, device: str, request_id: str, track: TrackInfo) -> list[dict]:
        return await asyncio.to_thread(self._impl.play_directives_sync, device, request_id, track)


def clean_title(title: str) -> str:
    """Срезаем хвосты вида «— Topic», «(Official Video)» из названий роликов."""
    t = re.sub(r"\s*[-–—]\s*(official\s+)?(audio|lyrics?|topic|vevo)\b.*$", "", title, flags=re.I)
    t = re.sub(r"\s*[(\[]\s*(official|lyrics?|audio|hq|4k|remaster(ed)?)[^\])]*[\])]\s*", " ", t, flags=re.I)
    return " ".join(t.split())
