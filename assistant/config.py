"""Configuration and well-known paths.

Non-secret settings come from ``config.toml`` at the project root. Secrets are
never stored here — see :mod:`assistant.secrets_store`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DB_PATH = DATA_DIR / "assistant.db"
CAPTURE_PATH = PROJECT_ROOT / "capture.txt"     # Phase 2
PROFILE_PATH = PROJECT_ROOT / "profile.md"      # Phase 4 classifier context
SUBMITTAL_WATCH_PATH = PROJECT_ROOT / "submittal_watch.txt"   # plain-text watch list
BRIEF_DIR = PROJECT_ROOT / "briefs"             # Phase 4 output

# Secret names (keys in the OS credential store under service_name()).
DEFAULT_SERVICE_NAME = "work-assistant"
SECRET_NOTION_TOKEN = "notion_token"
SECRET_IMAP_PASSWORD = "imap_password"
SECRET_GEMINI_API_KEY = "gemini_api_key"  # Phase 4


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def service_name() -> str:
    """Namespace for secrets in the OS credential store ([secrets] service_name)."""
    try:
        return load_config().get("secrets", {}).get("service_name", DEFAULT_SERVICE_NAME)
    except FileNotFoundError:
        return DEFAULT_SERVICE_NAME


def owner_name() -> str:
    """Name used in AI prompts ([user] name); generic wording if unset."""
    try:
        return load_config().get("user", {}).get("name") or "the user"
    except FileNotFoundError:
        return "the user"
