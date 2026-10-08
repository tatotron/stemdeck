"""Trust the OS certificate store when Python's Mozilla bundle is not enough.

A network that re-signs HTTPS puts its CA in the Windows certificate store.
Git with ``http.sslBackend schannel`` and the installed StemDeck app accept
that CA. This process does not: yt-dlp, requests and model downloads verify
against certifi, which has never seen the inspection CA, and fail with
``unable to get local issuer certificate``.

``truststore.inject_into_ssl()`` keeps verification on and asks the OS store
instead. It does not disable certificate checks.

On by default in a source checkout. Off in the desktop package (the app
directory's parent is named ``backend``) and under pytest, unless
``STEMDECK_TRUST_SYSTEM_CERTS`` says otherwise: ``1`` forces it on, ``0``
forces it off.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("stemdeck.certs")

_ON = frozenset({"1", "true", "on", "yes"})
_OFF = frozenset({"0", "false", "off", "no"})


def enabled() -> bool:
    flag = os.environ.get("STEMDECK_TRUST_SYSTEM_CERTS", "").strip().lower()
    if flag in _OFF:
        return False
    if flag in _ON:
        return True
    if "pytest" in sys.modules:
        return False
    # app/system_certs.py -> parent is app/, parent.parent is the repo, or in
    # the desktop package a directory named backend. Same layout test as
    # app.core.config._packaged_data_dir.
    return Path(__file__).resolve().parent.parent.name != "backend"


def install() -> None:
    """Replace ssl.SSLContext with truststore's, once, when enabled."""
    if not enabled():
        return
    try:
        import ssl

        import truststore
    except ImportError:
        logger.warning("truststore is not installed; TLS keeps the default CA bundle")
        return
    if ssl.SSLContext is truststore.SSLContext:
        return
    truststore.inject_into_ssl()
    logger.info("TLS verification is using the operating system certificate store")
