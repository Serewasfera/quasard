"""Локальные команды, которые выполняем без LLM (быстро и надёжно)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .protocol import client_action

_NUMS = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "максимум": 10, "минимум": 1,
}


@dataclass
class IntentResult:
    text: str = ""  # что сказать (пусто — молча)
    directives: list[dict] = field(default_factory=list)
    reset_dialog: bool = False


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е")).split())


def _level(words: list[str]) -> int | None:
    for w in words:
        if w.isdigit():
            return int(w)
        if w in _NUMS:
            return _NUMS[w]
    return None


def match(text: str) -> IntentResult | None:
    t = _norm(text)
    t = re.sub(r"^(алиса|слушай алиса)\s+", "", t)
    words = t.split()
    if not words:
        return None

    if re.fullmatch(r"(стоп|хватит|замолчи|молчи|отмена|тихо|выключись|помолчи)( пожалуйста)?", t):
        return IntentResult()

    if re.search(r"\b(громкость|уровень громкости|поставь громкость|сделай громкость)\b", t):
        lvl = _level(words)
        if lvl is not None and 0 <= lvl <= 10:
            return IntentResult(directives=[client_action("sound_set_level",
                                                          {"new_level": lvl, "new_percent_level": lvl * 10})])
    _what = r"( музыку| музыка| звук| песню| трек)?"
    if re.fullmatch(rf"(сделай|сделать|можно)?{_what} ?(погромче|громче|прибавь|увеличь громкость|еще громче)"
                    rf"{_what}( пожалуйста)?", t):
        return IntentResult(directives=[client_action("sound_louder")])
    if re.fullmatch(rf"(сделай|сделать|можно)?{_what} ?(потише|тише|убавь|уменьши громкость|еще тише)"
                    rf"{_what}( пожалуйста)?", t):
        return IntentResult(directives=[client_action("sound_quiter")])  # именно quiter — так у Яндекса

    if re.search(r"\b(включи|запусти)\b.*\b(блютуз|bluetooth)\b", t):
        return IntentResult("Включаю блютуз.", [client_action("start_bluetooth")])
    if re.search(r"\b(выключи|отключи)\b.*\b(блютуз|bluetooth)\b", t):
        return IntentResult("Выключаю блютуз.", [client_action("stop_bluetooth")])

    if re.fullmatch(r"(давай )?(начнем сначала|новый (диалог|разговор)|забудь (все|наш разговор)|сбрось (диалог|контекст))", t):
        return IntentResult("Хорошо, начнём заново.", reset_dialog=True)
    return None


# ------------------------------------------------------------------ музыка

_PLAY = r"(включи|включай|поставь|запусти|сыграй|играй|давай послушаем|хочу послушать)"
_WAVE = r"(музыку|музычку|что нибудь|что-нибудь|мою волну|волну|мою музыку|любимое|радио)"


def match_music(text: str) -> tuple[str, str] | None:
    """Музыкальная команда -> (действие, запрос).

    Действия: wave, search, next, prev, pause, resume, what, like, dislike."""
    t = re.sub(r"^(алиса|слушай алиса)\s+", "", _norm(text))
    if not t:
        return None
    # «мне нравится», «эта песня мне не нравится»… — короткая фраза-оценка, а не вопрос («что тебе нравится?»)
    words = t.split()
    is_rating = len(words) <= 6 and words[0] not in ("что", "кто", "почему", "как", "какая", "какой", "какие") \
        and "тебе" not in words and not text.strip().endswith("?")
    if re.fullmatch(r"(поставь )?дизлайк|дизлайкни|убери (эту песню|этот трек)|не надо такое", t) or \
            (is_rating and re.search(r"\bне (нравится|понравилось|нравятся)\b", t)):
        return "dislike", ""
    if re.fullmatch(r"(поставь )?лайк|лайкни|добавь в (любимые|избранное|понравившиеся|мне нравится)|"
                    r"(классная|крутая|отличная) (песня|музыка|песенка)", t) or \
            (is_rating and re.search(r"\b(нравится|понравилось|нравятся)\b", t)):
        return "like", ""
    if re.fullmatch(r"(дальше|следующий( трек)?|следующую( песню)?|следующая( песня)?|переключи( трек| песню)?|"
                    r"другую( песню)?|пропусти|включи следующ\w+( трек| песню)?|некст)( пожалуйста)?", t):
        return "next", ""
    if re.fullmatch(r"(назад|предыдущ\w+( трек| песн\w+)?|верни( предыдущ\w+)?( трек| песню)?|включи предыдущ\w+( трек| песню)?)"
                    r"( пожалуйста)?", t):
        return "prev", ""
    if re.fullmatch(r"(пауза|на паузу|поставь на паузу|останови( музыку)?|выключи музыку)( пожалуйста)?", t):
        return "pause", ""
    if re.fullmatch(r"(продолжи|продолжай|играй дальше|сними с паузы|возобнови)( музыку)?( пожалуйста)?", t):
        return "resume", ""
    if re.fullmatch(r"(что|кто) (сейчас |это )?(играет|поет|за песня|за трек)|как называется (эта )?(песня|трек)", t):
        return "what", ""
    m = re.fullmatch(_PLAY + r"( пожалуйста)?( " + _WAVE + r")?( пожалуйста)?", t)
    if m:
        return "wave", ""
    # без глагола — частый ответ в продолжении диалога: «Музыку», «Мою волну давай»
    if re.fullmatch(r"(давай |ну )?(музыку|музычку|мою волну|волну|музыка)( давай| включи| пожалуйста)?", t):
        return "wave", ""
    m = re.fullmatch(_PLAY + r"( пожалуйста)?( песню| песни| трек| альбом| группу| исполнителя| плейлист)? (.+)", t)
    if m:
        query = re.sub(r"\s+пожалуйста$", "", m.group(4)).strip()
        if query and not re.fullmatch(r"(свет|блютуз|bluetooth|телевизор|будильник|таймер)\b.*", query):
            return "search", query
    return None
