"""Opt-in lyrics transcription for the Sheet panel.

Runs after separation, and again when a finished track asks for it. A failure
here never changes the job's stems or a lyrics.json that was already saved:
the transcript is checked before it is written, and the write itself replaces
the file in one step.

The heavy work is the existing Whisper worker (app/pipeline/transcribe.py).
This module only decides whether to call it, which language to force, and
whether the answer is safe to keep.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

from app.core.models import Job, JobCancelled, _set
from app.pipeline import transcribe as tr
from app.pipeline.lyrics_lookup import read_lyrics, write_lyrics

logger = logging.getLogger("stemdeck.playalong")

LANGUAGES = ("auto", "pt", "es", "en")
_STAGE = "Transcribing lyrics..."
# Whisper's last word can sit a little past the container duration.
_SLACK_SEC = 1.0
_STAMP = re.compile(r"[<\[](\d{1,3}):(\d{2}(?:\.\d+)?)[>\]]")


def normalize_language(value: object) -> str:
    """``auto``, ``pt``, ``es`` or ``en``. Anything else is a ValueError."""
    text = str(value or "auto").strip().lower()
    if text in ("por", "portuguese", "pt-br", "pt-pt"):
        return "pt"
    if text in ("spa", "spanish"):
        return "es"
    if text in ("eng", "english"):
        return "en"
    if text not in LANGUAGES:
        raise ValueError("language must be auto, pt, es, or en")
    return text


def transcribe_if_opted_in(job: Job, job_dir: Path) -> bool:
    """The import-time pass. No-op unless this job was created with the
    toggle on. Synced lyrics already on disk are kept, so a track LRCLIB
    already timed does not pay for Whisper. Never raises except cancel."""
    if not job.playalong:
        return False
    if _has_synced(job_dir):
        _set(job, playalong_status="skipped")
        logger.info("[%s] play-along transcription skipped: synced lyrics already saved", job.id)
        return False
    return _transcribe(job, job_dir, job.playalong_language or "auto", settle=False)


def transcribe_now(job: Job, job_dir: Path, language: str) -> bool:
    """The button on a finished track. Replaces lyrics.json only after a
    transcript passes the checks. Never raises except cancel."""
    return _transcribe(job, job_dir, normalize_language(language), settle=True)


def _has_synced(job_dir: Path) -> bool:
    entry = read_lyrics(job_dir)
    if not entry:
        return False
    synced = entry.get("synced")
    return isinstance(synced, str) and bool(synced.strip())


def _transcribe(job: Job, job_dir: Path, language: str, *, settle: bool) -> bool:
    if job.cancel_requested:
        raise JobCancelled()
    language = normalize_language(language)
    vocals = job_dir / "stems" / "vocals.wav"
    _set(job, playalong_status="running", stage=_STAGE)
    try:
        if not vocals.is_file():
            logger.info("[%s] play-along transcription skipped: no vocals stem", job.id)
            _fail(job, settle)
            return False
        # Looked up on the transcribe module at call time so a test's stub of
        # _spawn_worker_cmd is the command that actually runs.
        cmd = tr._spawn_worker_cmd(vocals, tr._device(job))
        if language != "auto":
            cmd += ["--language", language]
        result = tr._run_worker(job, cmd)
        if not transcript_is_usable(result, job.duration_sec, language):
            logger.info("[%s] play-along transcription rejected", job.id)
            _fail(job, settle)
            return False
        entry = tr.build_lyrics(result, job)
        if entry is None or not _lyrics_in_range(entry.get("synced"), job.duration_sec):
            logger.info("[%s] play-along transcription heard nothing usable", job.id)
            _fail(job, settle)
            return False
        if not write_lyrics(job, job_dir, entry):
            _fail(job, settle)
            return False
    except JobCancelled:
        _set(job, playalong_status="none")
        raise
    except Exception:
        logger.exception("[%s] play-along transcription failed", job.id)
        _fail(job, settle)
        return False
    if settle:
        _set(job, playalong_status="done", stage="Done")
    else:
        _set(job, playalong_status="done")
    logger.info("[%s] play-along lyrics written (%s)", job.id, language)
    return True


def _fail(job: Job, settle: bool) -> None:
    """The track stays done. A lyrics.json that was already there is untouched
    because nothing was written. ``settle`` puts the stage back to Done, which
    a finished track's button needs; the import pass leaves that to the runner."""
    if settle:
        _set(job, playalong_status="error", stage="Done")
    else:
        _set(job, playalong_status="error")


def transcript_is_usable(result: object, duration: float | None, language: str) -> bool:
    """Worker JSON with in-range, ordered times and a sane language."""
    if not isinstance(result, dict) or result.get("skipped"):
        return False
    segments = result.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    heard = result.get("language")
    if language != "auto":
        if heard != language:
            return False
    elif heard is not None and not (isinstance(heard, str) and re.fullmatch(r"[a-z]{2,3}", heard)):
        return False
    limit = _limit(duration)
    previous = -0.05
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        start = _time(segment.get("start"))
        end = _time(segment.get("end"))
        if start is None or end is None or end < start or start < previous - 0.05:
            return False
        if start > limit or end > limit:
            return False
        previous = start
        word_at = start - 0.05
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                return False
            w0 = _time(word.get("start"))
            w1 = _time(word.get("end"))
            if w0 is None or w1 is None or w1 < w0 or w0 < word_at - 0.05:
                return False
            if w0 > limit or w1 > limit:
                return False
            word_at = w0
    return True


def _lyrics_in_range(synced: object, duration: float | None) -> bool:
    if not isinstance(synced, str) or not synced.strip():
        return False
    stamps = [int(m.group(1)) * 60 + float(m.group(2)) for m in _STAMP.finditer(synced)]
    if len(stamps) < 2:
        return False
    limit = _limit(duration)
    previous = -0.05
    for stamp in stamps:
        if stamp < 0 or stamp > limit or stamp < previous - 0.05:
            return False
        previous = stamp
    return True


def _limit(duration: float | None) -> float:
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
        return float(duration) + _SLACK_SEC
    return 24 * 3600


def _time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None
