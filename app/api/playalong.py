"""On-demand lyrics transcription for a finished track.

The import-time pass lives in the pipeline (app/pipeline/playalong.py). This
is the button: the same checks, on a job that is already done, and it never
shares the GPU with a separation.
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
from app.core.settings import get_playalong_language
from app.pipeline.playalong import normalize_language, transcribe_now
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


@router.post("/jobs/{job_id}/playalong/transcribe")
async def transcribe_playalong(job_id: str, request: Request) -> JSONResponse:
    """Transcribe the vocals of a finished job. lyrics.json is replaced only
    when the transcript's timestamps check out. A rejection leaves the file
    that was already there, and the job stays done."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw = body.get("language", get_playalong_language())
    try:
        language = normalize_language(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not _claim(job_id):
        raise HTTPException(status_code=409, detail="transcription already running")
    job_dir = (config.JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(config.JOBS_DIR.resolve()):
        _release(job_id)
        raise HTTPException(status_code=404, detail="job not found")
    try:
        async with _pipeline_lock:
            ok = await asyncio.to_thread(transcribe_now, job, job_dir, language)
    except JobCancelled:
        registry_persist(config.JOBS_DIR)
        raise HTTPException(status_code=409, detail="transcription cancelled") from None
    finally:
        _release(job_id)
    registry_persist(config.JOBS_DIR)
    return JSONResponse(
        {"ok": ok, "playalong_status": job.playalong_status},
        status_code=200,
    )
