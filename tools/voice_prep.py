"""Подготовка образца голоса для клонирования: расшифровка + выбор лучших фрагментов.

    .venv/Scripts/python tools/voice_prep.py "запись.mp3" [--out voice] [--lengths 10 15 25]

Делает:
  1. Silero VAD размечает речь, GigaAM расшифровывает её с таймкодами -> transcript.txt / transcript.srt
  2. Ищет окна нужной длины с лучшими признаками для клонирования:
     сплошная речь (мало пауз), ровная громкость, речь заметно громче фона, нормальный темп, края — на паузах
  3. Вырезает лучшие фрагменты в WAV (исходная частота, моно) + .txt с текстом фрагмента (нужен F5-TTS и др.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from alice_server.asr import Recognizer, _get_silero  # noqa: E402
from alice_server.config import load_config  # noqa: E402

SR = 16000
WIN = 512  # окно VAD, 32 мс


def decode(path: str, rate: int | None) -> tuple[np.ndarray, int]:
    with av.open(path) as c:
        st = c.streams.audio[0]
        rate = rate or st.rate
        rs = av.AudioResampler(format="flt", layout="mono", rate=rate)
        out = []
        for fr in c.decode(st):
            fr.pts = None
            out += [x.to_ndarray().reshape(-1) for x in rs.resample(fr)]
        out += [x.to_ndarray().reshape(-1) for x in rs.resample(None)]
    return np.concatenate(out), rate


def segments(probs: np.ndarray, thr=0.5, min_gap=0.35, max_len=20.0) -> list[tuple[float, float]]:
    """Отрезки речи (сек) по вероятностям VAD; короткие паузы склеиваем, длинные куски режем по паузам."""
    sp = probs >= thr
    t = WIN / SR
    segs, start = [], None
    for i, s in enumerate(sp):
        if s and start is None:
            start = i
        if not s and start is not None:
            segs.append([start * t, i * t])
            start = None
    if start is not None:
        segs.append([start * t, len(sp) * t])
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < min_gap and b - merged[-1][0] <= max_len:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= 0.3]


def fmt(sec: float, srt=False) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{s:06.3f}".replace(".", "," if srt else ".")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", default="voice")
    ap.add_argument("--lengths", type=float, nargs="+", default=[10, 15, 25])
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("Декодирование...", flush=True)
    pcm16, _ = decode(args.audio, SR)
    full, full_sr = decode(args.audio, None)
    print(f"  {len(pcm16) / SR / 60:.1f} мин, исходная частота {full_sr} Гц", flush=True)

    print("VAD...", flush=True)
    vad = _get_silero()
    n = len(pcm16) // WIN
    norm = pcm16 / max(1e-6, np.abs(pcm16).max())
    probs = np.concatenate([vad.probs(norm[i:i + WIN * 4096], vad_state)
                            for vad_state in [vad.new_state()] for i in range(0, n * WIN, WIN * 4096)])[:n]
    frames = pcm16[:n * WIN].reshape(n, WIN)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    segs = segments(probs)
    print(f"  отрезков речи: {len(segs)}", flush=True)

    print("Расшифровка GigaAM...", flush=True)
    rec = Recognizer(load_config().asr)
    rec.load()
    trans = []
    for k, (a, b) in enumerate(segs):
        text = rec._transcribe_local(pcm16[int(a * SR):int(b * SR)])
        trans.append((a, b, text))
        if k % 50 == 0:
            print(f"  {k}/{len(segs)} {fmt(a)} {text[:60]}", flush=True)
    with open(out / "transcript.txt", "w", encoding="utf-8") as f:
        f.writelines(f"[{fmt(a)} - {fmt(b)}] {t}\n" for a, b, t in trans if t)
    with open(out / "transcript.srt", "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate([x for x in trans if x[2]], 1):
            f.write(f"{i}\n{fmt(a, True)} --> {fmt(b, True)}\n{t}\n\n")

    print("Поиск лучших фрагментов...", flush=True)
    sp = probs >= 0.5
    fps = SR / WIN
    report = ["Фрагменты для клонирования (лучшие сверху). Оценки: доля речи, макс. пауза, ровность громкости,",
              "отношение речь/фон, темп (слов/с). Края фрагментов стоят на паузах между фразами.\n"]
    for L in args.lengths:
        cands = []
        # окно начинается с начала отрезка речи и заканчивается на конце отрезка (без обрезанных слов)
        for i, (a, _, _) in enumerate(trans):
            j = i
            while j < len(trans) and trans[j][1] - a < L:
                j += 1
            if j >= len(trans) or trans[j][1] - a > L * 1.25:
                continue
            b = trans[j][1]
            fa, fb = int(a * fps), int(b * fps)
            w_sp, w_db = sp[fa:fb], db[fa:fb]
            if len(w_sp) < 10:
                continue
            speech_ratio = w_sp.mean()
            # самая длинная пауза внутри окна
            gaps, run = [0], 0
            for s in w_sp:
                run = 0 if s else run + 1
                gaps.append(run)
            max_gap = max(gaps) / fps
            sp_db = w_db[w_sp]
            stab = np.std(sp_db) if len(sp_db) else 99
            ctx = db[max(0, fa - int(5 * fps)):fb + int(5 * fps)]
            ctx_sp = sp[max(0, fa - int(5 * fps)):fb + int(5 * fps)]
            noise = np.percentile(ctx[~ctx_sp], 20) if (~ctx_sp).sum() > 5 else np.percentile(ctx, 5)
            snr = np.median(sp_db) - noise if len(sp_db) else 0
            words = sum(len(t.split()) for x, y, t in trans[i:j + 1])
            rate = words / (b - a)
            clipping = (np.abs(full[int(a * full_sr):int(b * full_sr)]) > 0.99).mean()
            score = (2.0 * speech_ratio - 1.0 * max(0, max_gap - 0.6) - 0.08 * stab + 0.03 * min(snr, 40)
                     - 0.5 * abs(np.clip(rate, 1.8, 3.6) - rate) - 50 * clipping)
            cands.append((score, a, b, speech_ratio, max_gap, stab, snr, rate,
                          " ".join(t for _, _, t in trans[i:j + 1] if t)))
        cands.sort(reverse=True)
        chosen = []
        for c in cands:
            if all(c[2] <= x[1] or c[1] >= x[2] for x in chosen):
                chosen.append(c)
            if len(chosen) >= args.top:
                break
        report.append(f"=== {L:.0f} с ===")
        for k, (score, a, b, r, g, st, snr, rate, text) in enumerate(chosen, 1):
            name = f"ref_{int(L)}s_{k}"
            clip = full[int(a * full_sr):int(b * full_sr)]
            clip = clip / max(1e-6, np.abs(clip).max()) * 0.9
            with av.open(str(out / f"{name}.wav"), "w") as o:
                s = o.add_stream("pcm_s16le", rate=full_sr)
                s.layout = "mono"
                fr = av.AudioFrame.from_ndarray((clip * 32767).astype(np.int16).reshape(1, -1), format="s16",
                                                layout="mono")
                fr.sample_rate = full_sr
                for p in s.encode(fr):
                    o.mux(p)
                for p in s.encode(None):
                    o.mux(p)
            (out / f"{name}.txt").write_text(text, encoding="utf-8")
            report.append(f"{name}.wav  [{fmt(a)} - {fmt(b)}] {b - a:.1f}с  оценка {score:.2f} | речь {r:.0%}, "
                          f"пауза {g:.2f}с, ровность {st:.1f} дБ, речь/фон {snr:.0f} дБ, темп {rate:.1f} сл/с\n"
                          f"    «{text}»")
        report.append("")
    (out / "best_fragments.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()
