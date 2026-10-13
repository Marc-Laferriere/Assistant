"""Logging: human-readable to stderr, full detail to a rotating file."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, ensure_dirs

_CONFIGURED = False

# Third-party libraries whose DEBUG output is wire-protocol noise, not signal -
# imapclient in particular dumps every raw IMAP line, which alone can blow
# through a rotation threshold in one busy session.
_QUIET_LOGGERS = ("imapclient", "imapclient.imaplib", "keyring")


class _SafeRotatingFileHandler(RotatingFileHandler):
    """Windows can hold the log file locked (another instance, AV scan) right
    when a rotation is due. Skip that one rotation instead of raising - the
    file just carries on a bit past maxBytes until the next chance succeeds."""

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except OSError:
            pass


def setup_logging(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    ensure_dirs()

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Under pythonw.exe (the hidden scheduled runs) sys.stderr is None, not just
    # redirected - a plain StreamHandler() would crash on the first log call.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(console)

    logfile = _SafeRotatingFileHandler(
        LOG_DIR / "assistant.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(logfile)

    _CONFIGURED = True
