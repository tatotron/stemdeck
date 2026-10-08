"""The OS trust-store hook stays off in tests unless a test asks for it."""

from __future__ import annotations

import ssl

import truststore

from app.system_certs import enabled, install


def test_unset_stays_off_under_pytest(monkeypatch):
    monkeypatch.delenv("STEMDECK_TRUST_SYSTEM_CERTS", raising=False)
    assert enabled() is False


def test_explicit_off(monkeypatch):
    monkeypatch.setenv("STEMDECK_TRUST_SYSTEM_CERTS", "0")
    assert enabled() is False


def test_explicit_on_even_under_pytest(monkeypatch):
    monkeypatch.setenv("STEMDECK_TRUST_SYSTEM_CERTS", "1")
    assert enabled() is True


def test_install_respects_the_flag(monkeypatch):
    monkeypatch.setenv("STEMDECK_TRUST_SYSTEM_CERTS", "0")
    truststore.extract_from_ssl()
    install()
    assert ssl.SSLContext is not truststore.SSLContext

    monkeypatch.setenv("STEMDECK_TRUST_SYSTEM_CERTS", "1")
    try:
        install()
        assert ssl.SSLContext is truststore.SSLContext
    finally:
        truststore.extract_from_ssl()
