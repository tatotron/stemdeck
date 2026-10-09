"""Chord symbols for a finished track.

Detection is the slow part and shares the pipeline lock with separation.
Editing one symbol is a file rewrite: a user-typed symbol is flagged and a
later detection leaves it alone.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core import config
from app.core.config import JOB_ID_RE
from app.core.models import JobCancelled
from app.core.registry import get as registry_get
from app.core.registry import persist as registry_persist
from app.pipeline.chords import detect_now, edit_chord, read_chords
from app.pipeline.runner import _pipeline_lock

router = APIRouter()

_guard = threading.Lock()
_busy: set[str] = set()


def _claim(job_id: str) -> bool:
    with _guard:
        if job_id in _busy:
            return False
        _busy.add(job_id)
        return True


def _release(job_id: str) -> None:
    with _guard:
        _busy.discard(job_id)


def _job(job_id: str):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not found")
    job_dir = (config.JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(config.JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    return job, job_dir


@router.get("/jobs/{job_id}/chords")
def get_chords(job_id: str) -> JSONResponse:
    _job_obj, job_dir = _job(job_id)
    data = read_chords(job_dir)
    if data is None:
        raise HTTPException(status_code=404, detail="no chords")
    return JSONResponse(data)


@router.put("/jobs/{job_id}/chords")
async def put_chord(job_id: str, request: Request) -> JSONResponse:
    """Correct the symbol that covers ``time``. American notation only."""
    _job_obj, job_dir = _job(job_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="chord must be an American symbol such as D, Bm7, G/B")
    try:
        chord = edit_chord(job_dir, body.get("time"), body.get("symbol"), _job_obj.duration_sec)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return JSONResponse(chord)


@router.post("/jobs/{job_id}/chords/detect")
async def detect_chords(job_id: str) -> JSONResponse:
    """Find chords on a finished job. User-edited symbols are kept."""
    job, job_dir = _job(job_id)
    if not _claim(job_id):
        raise HTTPException(status_code=409, detail="chord detection already running")
    try:
        async with _pipeline_lock:
            ok = await asyncio.to_thread(detect_now, job, job_dir)
    except JobCancelled:
        registry_persist(config.JOBS_DIR)
        raise HTTPException(status_code=409, detail="chord detection cancelled") from None
    finally:
        _release(job_id)
    registry_persist(config.JOBS_DIR)
    data = read_chords(job_dir) if ok else None
    return JSONResponse(
        {"ok": ok, "chords_status": job.chords_status, "chords": (data or {}).get("chords", [])},
        status_code=200,
    )
