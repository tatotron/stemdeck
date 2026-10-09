"""Beat-synced chords for the Sheet panel.

Chroma from the harmonic stems (guitar, piano, other), one label per beat,
American symbols. A 7th or sus is kept only when it beats the plain triad.
A slash bass note is added only when the bass stem holds a chord tone.

A failure here never changes the job. chords.json is replaced only after the
labels and times check out, and a symbol the user typed is never overwritten
by a later detection.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path

import numpy as np

from app.core.models import Job, JobCancelled, _set

logger = logging.getLogger("stemdeck.chords")

_STAGE = "Finding chords..."
_SR = 22050
_HOP = 2048
# An extended chord has to beat the best triad by this much (cosine).
# Tight, because a busy guitar voicing otherwise reads as a 7th all the time.
_EXT_MARGIN = 0.08
# Keep the previous symbol when it is still this close on the new beat.
_HOLD = 0.05
# Bonus for a chord that belongs to the analyzed key. Small on purpose:
# a clear chromatic chord still wins.
_DIATONIC = 0.04
# Bass pitch class must lead the bass chroma by this ratio, and be a chord tone.
_BASS_RATIO = 1.45
_SILENCE = 1.5
_MIN_PIECE = 0.12
_SLACK = 1.0

_SHARP = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
_PC = {name: i for i, name in enumerate(_SHARP)}
_PC.update({name: i for i, name in enumerate(_FLAT)})
# Major-key accidentals, as fifths from C. Negative means flats.
_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: 7, 5: -1, 10: -2, 3: -3, 8: -4}

# (intervals, canonical suffix, triad?)
_TEMPLATES: tuple[tuple[tuple[int, ...], str, bool], ...] = (
    ((0, 4, 7), "", True),
    ((0, 3, 7), "m", True),
    ((0, 4, 7, 10), "7", False),
    ((0, 3, 7, 10), "m7", False),
    ((0, 4, 7, 11), "maj7", False),
    ((0, 3, 6, 10), "m7(b5)", False),
    ((0, 2, 7), "sus2", False),
    ((0, 5, 7), "sus4", False),
)
_INTERVALS = {suffix: intervals for intervals, suffix, _triad in _TEMPLATES}

# Longest quality first: the pattern is alternation, not longest-match.
_SYMBOL = re.compile(
    r"^(?P<root>[A-G](?:#|b)?)"
    r"(?P<qual>m7\(b5\)|m7b5|min7b5|7sus4|maj7|ma7|M7|min7|mi7|m7|dim7|"
    r"sus2|sus4|add9|maj9|min9|m9|sus|maj|min|mi|dim|aug|m|\+|°|7|6|9)?$"
)
_CANON = {
    "": "",
    "m": "m",
    "min": "m",
    "mi": "m",
    "maj": "",
    "7": "7",
    "maj7": "maj7",
    "ma7": "maj7",
    "M7": "maj7",
    "m7": "m7",
    "min7": "m7",
    "mi7": "m7",
    "m7(b5)": "m7(b5)",
    "m7b5": "m7(b5)",
    "min7b5": "m7(b5)",
    "dim": "dim",
    "°": "dim",
    "dim7": "dim7",
    "aug": "aug",
    "+": "aug",
    "sus": "sus4",
    "sus2": "sus2",
    "sus4": "sus4",
    "7sus4": "7sus4",
    "6": "6",
    "9": "9",
    "add9": "add9",
    "maj9": "maj9",
    "m9": "m9",
    "min9": "m9",
}
_SLASH = re.compile(
    r"^(?P<body>.+?)/(?P<bass>[A-G](?:#|b)?)$"
)

_lock = threading.Lock()


def normalize_symbol(value: object) -> str:
    """An American chord symbol, or ValueError.

    ``Bm7``, ``F#m7(b5)``, ``G/B``, ``Asus4``, ``D``. The letters the user
    typed are kept (``Bb`` stays ``Bb``); only the quality word is folded
    onto one spelling.
    """
    text = str(value or "").strip().replace(" ", "")
    if text:
        text = text[0].upper() + text[1:]
        if "/" in text:
            body, bass = text.split("/", 1)
            if bass:
                text = body + "/" + bass[0].upper() + bass[1:]
    if not text or len(text) > 16:
        raise ValueError("chord must be an American symbol such as D, Bm7, F#m7(b5), G/B")
    bass = None
    slash = _SLASH.fullmatch(text)
    if slash:
        text = slash.group("body")
        bass = slash.group("bass")
    match = _SYMBOL.fullmatch(text)
    if match is None:
        raise ValueError("chord must be an American symbol such as D, Bm7, F#m7(b5), G/B")
    root = _note(match.group("root"))
    if root is None:
        raise ValueError("chord must be an American symbol such as D, Bm7, F#m7(b5), G/B")
    qual = _CANON[match.group("qual") or ""]
    symbol = root + qual
    if bass:
        bass_note = _note(bass)
        if bass_note is None:
            raise ValueError("chord must be an American symbol such as D, Bm7, F#m7(b5), G/B")
        if _PC[bass_note] != _PC[root]:
            symbol += "/" + bass_note
    return symbol


def _note(text: str) -> str | None:
    letter = text[0].upper()
    accidental = text[1:] if len(text) > 1 else ""
    name = letter + accidental
    if name not in _PC:
        return None
    return name


def detect_if_opted_in(job: Job, job_dir: Path) -> bool:
    """The import-time pass. No-op unless this job was created with the
    toggle on. Never raises except cancel."""
    if not job.chords:
        return False
    return _detect(job, job_dir, settle=False)


def detect_now(job: Job, job_dir: Path) -> bool:
    """The button on a finished track. Never raises except cancel."""
    return _detect(job, job_dir, settle=True)


def edit_chord(job_dir: Path, time: float, symbol: str, duration: float | None) -> dict:
    """Replace the symbol that covers ``time``. The chord is marked user-edited.
    Raises ValueError when the symbol or the file is no good."""
    symbol = normalize_symbol(symbol)
    if isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time):
        raise ValueError("time is not a beat")
    with _lock:
        data = read_chords(job_dir)
        if data is None:
            raise ValueError("no chords")
        target = _find(data["chords"], float(time))
        if target is None:
            raise ValueError("no chord there")
        target["symbol"] = symbol
        target["user"] = True
        if not chords_are_usable(data, duration):
            raise ValueError("chord change did not check out")
        _write(job_dir, data)
    return target


def read_chords(job_dir: Path) -> dict | None:
    path = job_dir / "stems" / "chords.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("chords"), list):
        return None
    return data


def chords_are_usable(data: object, duration: float | None) -> bool:
    if not isinstance(data, dict) or data.get("v") != 1:
        return False
    chords = data.get("chords")
    if not isinstance(chords, list):
        return False
    limit = _limit(duration)
    previous_end = 0.0
    for chord in chords:
        if not isinstance(chord, dict) or not isinstance(chord.get("user"), bool):
            return False
        try:
            normalize_symbol(chord.get("symbol"))
        except ValueError:
            return False
        start = _time(chord.get("time"))
        end = _time(chord.get("end"))
        if start is None or end is None or end <= start or start < 0 or end > limit:
            return False
        if start < previous_end - 1e-3:
            return False
        previous_end = end
    return True


def merge_user(detected: list[dict], previous: list[dict] | None) -> list[dict]:
    """Detected spans with the user's symbols laid on top. A user span is
    kept whole, and a detected span is trimmed around it."""
    user = [dict(c) for c in (previous or []) if isinstance(c, dict) and c.get("user") is True]
    pieces: list[dict] = [dict(c) for c in detected]
    for kept in user:
        nxt: list[dict] = []
        for piece in pieces:
            nxt.extend(_cut(piece, kept))
        pieces = nxt
    pieces = [p for p in pieces if p["end"] - p["time"] >= _MIN_PIECE]
    return sorted([*pieces, *user], key=lambda c: (c["time"], c["end"]))


def _detect(job: Job, job_dir: Path, *, settle: bool) -> bool:
    if job.cancel_requested:
        raise JobCancelled()
    _set(job, chords_status="running", stage=_STAGE)
    try:
        beats, duration = load_beats(job_dir, job.duration_sec)
        if len(beats) < 4:
            logger.info("[%s] chords skipped: no beat grid", job.id)
            _fail(job, settle)
            return False
        harmonic, bass, sr = _mix(job_dir)
        if harmonic is None:
            logger.info("[%s] chords skipped: no harmonic stem", job.id)
            _fail(job, settle)
            return False
        if job.cancel_requested:
            raise JobCancelled()
        detected = chords_from_audio(
            harmonic,
            bass,
            sr,
            beats,
            duration,
            key=job.key,
            scale=job.scale,
        )
        if not chords_are_usable({"v": 1, "chords": detected}, duration):
            _fail(job, settle)
            return False
        with _lock:
            previous = read_chords(job_dir)
            merged = merge_user(detected, (previous or {}).get("chords") if previous else None)
            data = {"v": 1, "chords": merged}
            if not chords_are_usable(data, duration):
                _fail(job, settle)
                return False
            _write(job_dir, data)
    except JobCancelled:
        _set(job, chords_status="none")
        raise
    except Exception:
        logger.exception("[%s] chord detection failed", job.id)
        _fail(job, settle)
        return False
    if settle:
        _set(job, chords_status="done", stage="Done")
    else:
        _set(job, chords_status="done")
    logger.info("[%s] chords written", job.id)
    return True


def _fail(job: Job, settle: bool) -> None:
    if settle:
        _set(job, chords_status="error", stage="Done")
    else:
        _set(job, chords_status="error")


def load_beats(job_dir: Path, duration: float | None) -> tuple[list[float], float]:
    """Beat times. A user-edited grid wins over the detected one."""
    stems = job_dir / "stems"
    beats = _beat_list(stems / "beats.user.json") or _beat_list(stems / "beats.json")
    if not beats:
        return [], 0.0
    span = float(duration) if isinstance(duration, (int, float)) and duration and duration > beats[-1] else 0.0
    if span <= beats[-1]:
        step = float(np.median(np.diff(beats))) if len(beats) > 1 else 0.5
        span = beats[-1] + step
    return beats, span


def _beat_list(path: Path) -> list[float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("beats") if isinstance(data, dict) else None
    if not isinstance(raw, list) or len(raw) < 4:
        return None
    out: list[float] = []
    for item in raw:
        t = _time(item)
        if t is None or t < 0 or (out and t <= out[-1]):
            return None
        out.append(t)
    return out


def chords_from_audio(
    harmonic: np.ndarray,
    bass: np.ndarray | None,
    sr: int,
    beats: list[float],
    duration: float,
    *,
    key: str | None,
    scale: str | None,
) -> list[dict]:
    """One American symbol per run of beats. Empty when the audio is quiet."""
    import librosa

    harm = _prepare(harmonic, sr)
    low = _prepare(bass, sr) if bass is not None and bass.size else None
    if low is not None and low.size != harm.size:
        n = min(low.size, harm.size)
        harm = harm[:n]
        low = low[:n]
    chroma = librosa.feature.chroma_cqt(y=harm, sr=_SR, hop_length=_HOP, norm=None)
    bass_chroma = librosa.feature.chroma_cqt(y=low, sr=_SR, hop_length=_HOP, norm=None) if low is not None else None
    times = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=_SR, hop_length=_HOP)
    flats = _prefers_flats(key, scale)
    diatonic = _diatonic(key, scale)
    labels: list[str | None] = []
    previous: str | None = None
    for i, start in enumerate(beats):
        end = beats[i + 1] if i + 1 < len(beats) else duration
        window = (times >= start) & (times < end)
        if not np.any(window):
            labels.append(previous)
            continue
        column = chroma[:, window].mean(axis=1)
        symbol, score, table = _match(column, flats, diatonic)
        if symbol is None:
            labels.append(previous)
            continue
        held = previous.split("/", 1)[0] if previous else None
        if held is not None and table.get(held, -1.0) >= score - _HOLD:
            symbol = previous
        else:
            low_col = bass_chroma[:, window].mean(axis=1) if bass_chroma is not None else None
            symbol = _with_bass(symbol, low_col, flats)
        labels.append(symbol)
        previous = symbol
    return _spans(beats, _steady(labels), duration)


def _prepare(y: np.ndarray, sr: int) -> np.ndarray:
    import librosa

    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != _SR and y.size:
        y = librosa.resample(y, orig_sr=sr, target_sr=_SR)
    return np.ascontiguousarray(y, dtype=np.float32)


def _match(
    column: np.ndarray, flats: bool, diatonic: set[tuple[int, str]]
) -> tuple[str | None, float, dict[str, float]]:
    energy = float(np.sum(column))
    if energy < _SILENCE:
        return None, 0.0, {}
    vec = column / (np.linalg.norm(column) + 1e-9)
    best_triad: tuple[float, int, str] | None = None
    best_ext: tuple[float, int, str] | None = None
    table: dict[str, float] = {}
    for root in range(12):
        for intervals, suffix, triad in _TEMPLATES:
            tmpl = np.zeros(12, dtype=float)
            for interval in intervals:
                tmpl[(root + interval) % 12] = 1.0
            tmpl /= np.linalg.norm(tmpl)
            score = float(np.dot(vec, tmpl))
            if (root, suffix) in diatonic:
                score += _DIATONIC
            name = _spell(root, flats) + suffix
            table[name] = max(table.get(name, -1.0), score)
            slot = (score, root, suffix)
            if triad:
                if best_triad is None or slot[0] > best_triad[0]:
                    best_triad = slot
            elif best_ext is None or slot[0] > best_ext[0]:
                best_ext = slot
    assert best_triad is not None
    chosen = best_triad
    if best_ext is not None and best_ext[0] >= best_triad[0] + _EXT_MARGIN:
        chosen = best_ext
    score, root, suffix = chosen
    return _spell(root, flats) + suffix, score, table


def _with_bass(symbol: str, bass: np.ndarray | None, flats: bool) -> str:
    if bass is None or float(np.sum(bass)) < _SILENCE:
        return symbol
    root_name = symbol[0]
    if len(symbol) > 1 and symbol[1] in "#b":
        root_name = symbol[:2]
    root = _PC[root_name]
    qual = symbol[len(root_name):]
    tones = {(root + iv) % 12 for iv in _INTERVALS.get(qual, (0, 4, 7))}
    order = np.argsort(bass)[::-1]
    peak = int(order[0])
    second = float(bass[order[1]]) if bass.size > 1 else 0.0
    if peak == root or peak not in tones:
        return symbol
    if float(bass[peak]) < _BASS_RATIO * max(second, 1e-9):
        return symbol
    return f"{symbol}/{_spell(peak, flats)}"


def _steady(labels: list[str | None]) -> list[str | None]:
    """Drop a chord that lasts a single beat when a neighbour can take its place.
    One pass is not enough: filling a gap can leave a new one-beat island."""
    labels = list(labels)
    for _ in range(2):
        i = 0
        while i < len(labels):
            j = i + 1
            while j < len(labels) and labels[j] == labels[i]:
                j += 1
            if labels[i] is not None and j - i < 2:
                prev = labels[i - 1] if i else None
                nxt = labels[j] if j < len(labels) else None
                fill = prev if prev is not None else nxt
                if fill is not None:
                    labels[i:j] = [fill] * (j - i)
            i = j
    return labels


def _spans(beats: list[float], labels: list[str | None], duration: float) -> list[dict]:
    spans: list[dict] = []
    i = 0
    while i < len(labels):
        label = labels[i]
        if label is None:
            i += 1
            continue
        j = i + 1
        while j < len(labels) and labels[j] == label:
            j += 1
        end = beats[j] if j < len(beats) else duration
        start = beats[i]
        if end - start >= _MIN_PIECE:
            spans.append({"time": round(start, 4), "end": round(end, 4), "symbol": label, "user": False})
        i = j
    return spans


def _cut(piece: dict, user: dict) -> list[dict]:
    if piece["end"] <= user["time"] or user["end"] <= piece["time"]:
        return [piece]
    out = []
    if piece["time"] < user["time"]:
        out.append({**piece, "end": user["time"]})
    if user["end"] < piece["end"]:
        out.append({**piece, "time": user["end"]})
    return out


def _find(chords: list, time: float) -> dict | None:
    for chord in chords:
        if not isinstance(chord, dict):
            continue
        start = _time(chord.get("time"))
        if start is not None and abs(start - time) <= 0.05:
            return chord
    for chord in chords:
        if not isinstance(chord, dict):
            continue
        start = _time(chord.get("time"))
        end = _time(chord.get("end"))
        if start is None or end is None:
            continue
        if start <= time < end:
            return chord
    return None


def _mix(job_dir: Path) -> tuple[np.ndarray | None, np.ndarray | None, int]:
    import soundfile as sf

    stems = job_dir / "stems"
    parts: list[np.ndarray] = []
    sr = _SR
    for name in ("guitar", "piano", "other"):
        path = stems / f"{name}.wav"
        if not path.is_file():
            continue
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        y = data.mean(axis=1)
        if not parts:
            sr = int(rate)
        elif int(rate) != sr or y.size != parts[0].size:
            n = min(y.size, parts[0].size)
            y = y[:n]
            parts = [p[:n] for p in parts]
        parts.append(y)
    if not parts:
        return None, None, sr
    harmonic = np.sum(parts, axis=0, dtype=np.float32)
    bass_path = stems / "bass.wav"
    bass = None
    if bass_path.is_file():
        data, rate = sf.read(str(bass_path), dtype="float32", always_2d=True)
        bass = data.mean(axis=1)
        if int(rate) != sr:
            bass = None
        elif bass.size != harmonic.size:
            n = min(bass.size, harmonic.size)
            bass = bass[:n]
            harmonic = harmonic[:n]
    return harmonic, bass, sr


def _prefers_flats(key: str | None, scale: str | None) -> bool:
    parsed = _key(key, scale)
    if parsed is None:
        return False
    root, mode, _harmonic = parsed
    major = (root + 3) % 12 if mode == "min" else root
    return _FIFTHS.get(major, 0) < 0


def _diatonic(key: str | None, scale: str | None) -> set[tuple[int, str]]:
    parsed = _key(key, scale)
    if parsed is None:
        return set()
    tonic, mode, harmonic = parsed
    if mode == "maj":
        steps = (("", 0), ("m", 2), ("m", 4), ("", 5), ("", 7), ("m", 9), ("m7(b5)", 11))
        sevenths = (("maj7", 0), ("m7", 2), ("m7", 4), ("maj7", 5), ("7", 7), ("m7", 9), ("m7(b5)", 11))
    else:
        fifth = "" if harmonic else "m"
        fifth7 = "7" if harmonic else "m7"
        steps = (("m", 0), ("m7(b5)", 2), ("", 3), ("m", 5), (fifth, 7), ("", 8), ("", 10))
        sevenths = (("m7", 0), ("m7(b5)", 2), ("maj7", 3), ("m7", 5), (fifth7, 7), ("maj7", 8), ("7", 10))
    out = set()
    for suffix, step in (*steps, *sevenths):
        out.add(((tonic + step) % 12, suffix))
    return out


def _key(key: str | None, scale: str | None) -> tuple[int, str, bool] | None:
    if not isinstance(key, str) or not key.strip():
        return None
    parts = key.replace("-", "b").split()
    root = _note(parts[0])
    if root is None:
        return None
    mode = "min" if (len(parts) > 1 and parts[1].startswith("min")) or (
        isinstance(scale, str) and "inor" in scale
    ) else "maj"
    if len(parts) > 1 and parts[1].startswith("maj"):
        mode = "maj"
    harmonic = isinstance(scale, str) and "Harmonic" in scale
    return _PC[root], mode, harmonic


def _spell(pc: int, flats: bool) -> str:
    return (_FLAT if flats else _SHARP)[pc % 12]


def _write(job_dir: Path, data: dict) -> None:
    path = job_dir / "stems" / "chords.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _limit(duration: float | None) -> float:
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
        return float(duration) + _SLACK
    return 24 * 3600


def _time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None
