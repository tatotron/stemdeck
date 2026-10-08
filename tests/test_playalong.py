"""Play-along transcription: language, timestamp checks, and a failure that
leaves lyrics.json alone.

The worker is the suite's stub (conftest _no_whisper) unless a test points
_run_worker at an answer of its own. No model is loaded.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import playalong


def _segment(words):
    return {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in words],
    }


GOOD = {
    "language": "pt",
    "segments": [
        _segment(
            [
                {"word": "Seja", "start": 1.0, "end": 1.4},
                {"word": "mais", "start": 1.5, "end": 1.9},
                {"word": "você", "start": 2.0, "end": 2.6},
            ]
        )
    ],
}


def test_language_normalizes_names_and_rejects_the_rest():
    assert playalong.normalize_language("Portuguese") == "pt"
    assert playalong.normalize_language("spanish") == "es"
    assert playalong.normalize_language("EN") == "en"
    assert playalong.normalize_language("auto") == "auto"
    assert playalong.normalize_language("") == "auto"
    with pytest.raises(ValueError):
        playalong.normalize_language("fr")


def test_rejects_a_word_past_the_song():
    late = {
        "language": "en",
        "segments": [_segment([{"word": "no", "start": 50.0, "end": 50.4}])],
    }
    assert playalong.transcript_is_usable(late, 10.0, "auto") is False


def test_rejects_unsorted_words_and_a_forced_language_that_does_not_match():
    backwards = {
        "language": "en",
        "segments": [
            _segment(
                [
                    {"word": "b", "start": 2.0, "end": 2.2},
                    {"word": "a", "start": 1.0, "end": 1.2},
                ]
            )
        ],
    }
    assert playalong.transcript_is_usable(backwards, 30.0, "auto") is False
    assert playalong.transcript_is_usable(GOOD, 218.0, "en") is False
    assert playalong.transcript_is_usable(GOOD, 218.0, "pt") is True
    assert playalong.transcript_is_usable({"skipped": "language", "segments": []}, 30.0, "auto") is False


def test_opted_out_does_nothing(tmp_path):
    job = Job(id="abcdef00p001", status="done", playalong=False)
    assert playalong.transcribe_if_opted_in(job, tmp_path) is False
    assert job.playalong_status == "none"
    assert not (tmp_path / "lyrics.json").exists()


def test_opted_in_keeps_synced_lyrics(tmp_path):
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(
        '{"v":1,"source":"lrclib","track":"Seja","artist":"","album":"",'
        '"duration":10,"synced":"[00:01.00]ja","plain":"ja",'
        '"instrumental":false,"lrclib_id":1}\n',
        encoding="utf-8",
    )
    before = lyrics.read_bytes()
    job = Job(id="abcdef00p002", status="done", playalong=True, playalong_language="pt")
    assert playalong.transcribe_if_opted_in(job, tmp_path) is False
    assert job.playalong_status == "skipped"
    assert lyrics.read_bytes() == before


def test_a_failed_worker_does_not_touch_existing_lyrics(tmp_path):
    (tmp_path / "stems").mkdir()
    (tmp_path / "stems" / "vocals.wav").write_bytes(b"RIFF")
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_bytes(b'{"v":1,"synced":"[00:01.00]keep me"}\n')
    before = lyrics.read_bytes()
    job = Job(id="abcdef00p003", status="done", duration_sec=30.0, playalong_language="pt")
    assert playalong.transcribe_now(job, tmp_path, "pt") is False
    assert job.status == "done"
    assert job.playalong_status == "error"
    assert job.stage_message == "Done"
    assert lyrics.read_bytes() == before


def test_a_checked_transcript_is_written(tmp_path, monkeypatch):
    (tmp_path / "stems").mkdir()
    (tmp_path / "stems" / "vocals.wav").write_bytes(b"RIFF")
    monkeypatch.setattr("app.pipeline.transcribe._run_worker", lambda job, cmd: GOOD)
    job = Job(id="abcdef00p004", status="done", duration_sec=218.0, title="Seja")
    assert playalong.transcribe_now(job, tmp_path, "portuguese") is True
    assert job.playalong_status == "done"
    saved = (tmp_path / "lyrics.json").read_text(encoding="utf-8")
    assert '"source": "whisper"' in saved
    assert "Seja" in saved


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def test_settings_round_trip(client):
    assert client.get("/api/settings").json()["playalong"] is False
    assert client.get("/api/settings").json()["playalong_language"] == "auto"
    saved = client.post("/api/settings", json={"playalong": True, "playalong_language": "Portuguese"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["playalong"] is True
    assert body["playalong_language"] == "pt"
    refused = client.post("/api/settings", json={"playalong_language": "fr"})
    assert refused.status_code == 422
    assert client.get("/api/settings").json()["playalong_language"] == "pt"


def test_button_on_a_missing_job_is_404(client):
    assert client.post("/api/jobs/abcdef000009/playalong/transcribe", json={}).status_code == 404


def test_button_rejects_a_bad_language_and_a_missing_vocal(client):
    job = Job(id="abcdef000010", status="done", duration_sec=30.0)
    _jobs[job.id] = job
    bad = client.post(f"/api/jobs/{job.id}/playalong/transcribe", json={"language": "fr"})
    assert bad.status_code == 422
    assert job.playalong_status == "none"
    missed = client.post(f"/api/jobs/{job.id}/playalong/transcribe", json={"language": "en"})
    assert missed.status_code == 200
    assert missed.json()["ok"] is False
    assert missed.json()["playalong_status"] == "error"
    assert job.status == "done"
