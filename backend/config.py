"""
Central configuration for Blast Intelligence AI.

All paths are resolved relative to the project root so scripts work no matter
which directory they are launched from. Values can be overridden with
environment variables (prefixed ``BLAST_``) without editing code.

Privacy notes
-------------
* ``DATA_DIR`` holds confidential JSONL files. It is Git-ignored.
* ``REPORTS_DIR``, ``MODELS_DIR``, ``DB_PATH`` and ``LOGS_DIR`` hold artefacts
  derived from that data. They are Git-ignored too.
* The only AI endpoint is a *local* Ollama server. ``assert_local_url`` guards
  against accidentally pointing it at a remote host.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = BACKEND_DIR.parent


def _path_from_env(var: str, default: Path) -> Path:
    """Return a path from an environment variable, or ``default``."""
    value = os.environ.get(var)
    return Path(value).expanduser().resolve() if value else default


DATA_DIR: Path = _path_from_env("BLAST_DATA_DIR", PROJECT_ROOT / "data")
REPORTS_DIR: Path = _path_from_env("BLAST_REPORTS_DIR", PROJECT_ROOT / "local_reports")
MODELS_DIR: Path = _path_from_env("BLAST_MODELS_DIR", PROJECT_ROOT / "models")
LOGS_DIR: Path = _path_from_env("BLAST_LOGS_DIR", PROJECT_ROOT / "logs")
DB_PATH: Path = _path_from_env("BLAST_DB_PATH", PROJECT_ROOT / "local_db" / "blast.duckdb")

DATA_FILE_GLOB: str = "*.jsonl"

# ---------------------------------------------------------------------------
# Data-inspector settings
# ---------------------------------------------------------------------------
INSPECTOR_MAX_DEPTH: int = int(os.environ.get("BLAST_INSPECTOR_MAX_DEPTH", "8"))
"""Maximum nesting depth explored inside a record."""

INSPECTOR_MAX_ARRAY_ITEMS: int = int(os.environ.get("BLAST_INSPECTOR_MAX_ARRAY_ITEMS", "25"))
"""Array elements examined per array (the array length itself is always counted)."""

INSPECTOR_MAX_FIELDS_PER_FILE: int = int(os.environ.get("BLAST_INSPECTOR_MAX_FIELDS", "3000"))
"""Cap on distinct field paths tracked per file (protects against dynamic keys)."""

INSPECTOR_DISTINCT_CAP: int = int(os.environ.get("BLAST_INSPECTOR_DISTINCT_CAP", "10000"))
"""Distinct values counted per field (via in-memory hashes only) before stopping."""

INSPECTOR_PROGRESS_SECONDS: float = float(os.environ.get("BLAST_INSPECTOR_PROGRESS_SECONDS", "2.0"))
"""How often progress is printed while reading a file."""

# ---------------------------------------------------------------------------
# DuckDB / ingestion settings
# ---------------------------------------------------------------------------
DB_DIR: Path = DB_PATH.parent
DB_TEMP_DIR: Path = DB_DIR / "tmp"
"""DuckDB spill directory (kept inside the Git-ignored local_db/)."""

DUCKDB_THREADS: int = int(os.environ.get("BLAST_DUCKDB_THREADS", str(max((os.cpu_count() or 2) - 1, 1))))
DUCKDB_MEMORY_LIMIT: str = os.environ.get("BLAST_DUCKDB_MEMORY_LIMIT", "4GB")

INGEST_BATCH_SIZE: int = int(os.environ.get("BLAST_INGEST_BATCH_SIZE", "10000"))
"""Records per transaction during ingestion."""

INGEST_PROGRESS_SECONDS: float = float(os.environ.get("BLAST_INGEST_PROGRESS_SECONDS", "5.0"))
INGEST_MAX_ERRORS_STORED_PER_FILE: int = int(os.environ.get("BLAST_INGEST_MAX_ERRORS", "1000"))

INGESTION_CONFIG_PATH: Path = _path_from_env(
    "BLAST_INGESTION_CONFIG", BACKEND_DIR / "ingestion_config.json"
)
"""Real file->table mapping. Git-ignored because it will contain field names."""

INGESTION_CONFIG_EXAMPLE_PATH: Path = BACKEND_DIR / "ingestion_config.example.json"

LOG_LEVEL: str = os.environ.get("BLAST_LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Local LLM (Ollama) - used in later milestones
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL: str = os.environ.get("BLAST_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL: str = os.environ.get("BLAST_OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT_SECONDS: float = float(os.environ.get("BLAST_OLLAMA_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# Chat assistant / API (Milestone 7)
# ---------------------------------------------------------------------------
ANALYTICS_MAPPING_PATH: Path = _path_from_env("BLAST_ANALYTICS_MAPPING", REPORTS_DIR / "analytics_mapping.json")
LABEL_SALT_PATH: Path = DB_DIR / "label_salt.txt"
CHAT_USE_LLM: bool = os.environ.get("BLAST_CHAT_USE_LLM", "true").lower() != "false"
CHAT_REVEAL_LABELS: bool = os.environ.get("BLAST_CHAT_REVEAL_LABELS", "false").lower() == "true"
CHAT_MAX_QUESTION_CHARS: int = int(os.environ.get("BLAST_CHAT_MAX_QUESTION_CHARS", "1000"))
CHAT_MAX_BODY_BYTES: int = int(os.environ.get("BLAST_CHAT_MAX_BODY_BYTES", "16384"))
CHAT_RATE_LIMIT_PER_MINUTE: int = int(os.environ.get("BLAST_CHAT_RATE_LIMIT", "30"))
CHAT_QUERY_TIMEOUT_SECONDS: float = float(os.environ.get("BLAST_CHAT_QUERY_TIMEOUT", "20"))
CHAT_ROW_LIMIT: int = int(os.environ.get("BLAST_CHAT_ROW_LIMIT", "200"))
ANALYTICS_API_TIMEOUT_SECONDS: float = float(os.environ.get("BLAST_ANALYTICS_API_TIMEOUT", "60"))
ANALYTICS_API_RATE_LIMIT_PER_MINUTE: int = int(os.environ.get("BLAST_ANALYTICS_API_RATE_LIMIT", "120"))
CHAT_SESSION_TTL_SECONDS: int = int(os.environ.get("BLAST_CHAT_SESSION_TTL", "1800"))
CHAT_MAX_SESSIONS: int = int(os.environ.get("BLAST_CHAT_MAX_SESSIONS", "200"))
CHAT_HISTORY_TURNS: int = int(os.environ.get("BLAST_CHAT_HISTORY_TURNS", "6"))
API_HOST: str = os.environ.get("BLAST_API_HOST", "127.0.0.1")
API_PORT: int = int(os.environ.get("BLAST_API_PORT", "8000"))
API_CORS_ORIGINS: list[str] = [o.strip() for o in os.environ.get(
    "BLAST_API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000").split(",") if o.strip()]

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def assert_local_url(url: str) -> str:
    """Raise ``ValueError`` if ``url`` does not point at this machine.

    Use this before sending any company data to an HTTP endpoint.
    """
    host = urlparse(url).hostname
    if host not in _LOCAL_HOSTS:
        raise ValueError(
            f"Refusing non-local endpoint '{host}'. Company data must stay on this machine."
        )
    return url


def ensure_local_dirs() -> None:
    """Create the Git-ignored output directories if they do not exist."""
    for directory in (REPORTS_DIR, MODELS_DIR, LOGS_DIR, DB_DIR, DB_TEMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
