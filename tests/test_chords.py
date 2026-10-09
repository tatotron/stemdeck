"""Chord symbols: American notation, user edits, and a detection that can fail
without touching a file that is already there.

The audio matcher is stubbed except for one short synthetic chord.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import chords


def test_american_symbols():
    assert chords.normalize_symbol("D") == "D"
    assert chords.normalize_symbol("bm7") == "Bm7"
    assert chords.normalize_symbol("F#m7b5") == "F#m7(b5)"
    assert chords.normalize_symbol("F#m7(b5)") == "F#m7(b5)"
    assert chords.normalize_symbol("G/B") == "G/B"
    assert chords.normalize_symbol("Asus4") == "Asus4"
    assert chords.normalize_symbol("Bbmaj7") == "Bbmaj7"
    assert chords.normalize_symbol("D/D") == "D"
    for bad in ("Ré", "H7", "C major", "", "N.C.", "IVmaj7"):
        with pytest.raises(ValueError):
            chords.normalize_symbol(bad)


def test_user_edit_is_kept_and_the_detected_span_is_trimmed():
    detected = [
        {"time": 0.0, "end": 4.0, "symbol": "D", "user": False},
        {"time": 4.0, "end": 8.0, "symbol": "A7", "user": False},
    ]
    previous = [{"time": 2.0, "end": 4.0, "symbol": "Bm7", "user": True}]
    merged = chords.merge_user(detected, previous)
    assert [(c["symbol"], c["user"], c["time"], c["end"]) for c in merged] == [
        ("D", False, 0.0, 2.0),
        ("Bm7", True, 2.0, 4.0),
        ("A7", False, 4.0, 8.0),
    ]


def test_opted_out_does_nothing(tmp_path):
    job = Job(id="abcdef0000c1", status="done", chords=False)
    assert chords.detect_if_opted_in(job, tmp_path) is False
    assert job.chords_status == "none"
    assert not (tmp_path / "stems" / "chords.json").exists()


def test_a_failed_detection_leaves_an_existing_file(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    path = stems / "chords.json"
    path.write_text('{"v":1,"chords":[]}\n', encoding="utf-8")
    before = path.read_bytes()
    job = Job(id="abcdef0000c2", status="done", duration_sec=10.0)
    assert chords.detect_now(job, tmp_path) is False
    assert job.status == "done"
    assert job.chords_status == "error"
    assert path.read_bytes() == before


def test_redetect_does_not_overwrite_a_user_symbol(tmp_path, monkeypatch):
    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "beats.json").write_text(json.dumps({"beats": [0, 1, 2, 3, 4]}), encoding="utf-8")
    (stems / "chords.json").write_text(
        json.dumps({"v": 1, "chords": [{"time": 0.0, "end": 2.0, "symbol": "Bm7", "user": True}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(chords, "_mix", lambda job_dir: (np.zeros(8, dtype=np.float32), None, 22050))
    monkeypatch.setattr(
        chords,
        "chords_from_audio",
        lambda *args, **kwargs: [{"time": 0.0, "end": 4.0, "symbol": "D", "user": False}],
    )
    job = Job(id="abcdef0000c3", status="done", duration_sec=4.0)
    assert chords.detect_now(job, tmp_path) is True
    saved = json.loads((stems / "chords.json").read_text(encoding="utf-8"))
    assert saved["chords"][0] == {"time": 0.0, "end": 2.0, "symbol": "Bm7", "user": True}
    assert saved["chords"][1]["symbol"] == "D"
    assert saved["chords"][1]["user"] is False
    assert saved["chords"][1]["time"] == 2.0


def test_a_d_major_triad_is_named_d():
    sr = 22050
    t = np.arange(sr * 2, dtype=np.float32) / sr
    # D4, F#4, A4.
    harmonic = (
        np.sin(2 * np.pi * 293.66 * t) + np.sin(2 * np.pi * 369.99 * t) + np.sin(2 * np.pi * 440.0 * t)
    ).astype(np.float32)
    found = chords.chords_from_audio(
        harmonic, None, sr, [0.0, 1.0, 2.0, 2.5], 2.5, key="D maj", scale="Major"
    )
    assert found
    assert found[0]["symbol"] == "D"
    assert found[0]["user"] is False


def test_slash_bass_uses_the_bass_stem():
    sr = 22050
    t = np.arange(sr * 2, dtype=np.float32) / sr
    harmonic = (
        np.sin(2 * np.pi * 293.66 * t) + np.sin(2 * np.pi * 369.99 * t) + np.sin(2 * np.pi * 440.0 * t)
    ).astype(np.float32)
    bass = np.sin(2 * np.pi * 185.0 * t).astype(np.float32)  # F#3
    found = chords.chords_from_audio(
        harmonic, bass, sr, [0.0, 1.0, 2.0, 2.5], 2.5, key="D maj", scale="Major"
    )
    assert found
    assert found[0]["symbol"] == "D/F#"


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def test_settings_round_trip(client):
    assert client.get("/api/settings").json()["chords"] is False
    saved = client.post("/api/settings", json={"chords": True})
    assert saved.status_code == 200
    assert saved.json()["chords"] is True


def test_edit_rejects_a_bad_symbol_and_keeps_the_file(client):
    import app.core.config as cfg

    job = Job(id="abcdef0000c4", status="done", duration_sec=8.0)
    _jobs[job.id] = job
    stems = cfg.JOBS_DIR / job.id / "stems"
    stems.mkdir(parents=True)
    path = stems / "chords.json"
    path.write_text(
        json.dumps({"v": 1, "chords": [{"time": 1.0, "end": 3.0, "symbol": "D", "user": False}]}),
        encoding="utf-8",
    )
    before = path.read_bytes()
    bad = client.put(f"/api/jobs/{job.id}/chords", json={"time": 1.0, "symbol": "Ré"})
    assert bad.status_code == 422
    assert path.read_bytes() == before
    ok = client.put(f"/api/jobs/{job.id}/chords", json={"time": 1.0, "symbol": "bm7"})
    assert ok.status_code == 200
    assert ok.json()["symbol"] == "Bm7"
    assert ok.json()["user"] is True
    missing = client.post("/api/jobs/abcdef0000c9/chords/detect")
    assert missing.status_code == 404
