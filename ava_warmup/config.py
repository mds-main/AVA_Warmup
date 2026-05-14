"""Configuration loading for the standalone warm-up app."""

from __future__ import annotations

import os
from typing import Any

from .schemas import AppConfig


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped or default


def _env_optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _default_history_dir() -> str:
    explicit = os.getenv("AVA_WARMUP_HISTORY_DIR") or os.getenv("GC_TESTER_HISTORY_DIR")
    if explicit and explicit.strip():
        return explicit.strip()
    # On DigitalOcean App Platform the working directory is ephemeral; /tmp is
    # writable and survives container lifetime. Fall back to /tmp when the
    # platform marker is set, otherwise keep the local working-dir default.
    if os.getenv("AVA_WARMUP_USE_TMP_HISTORY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "/tmp/ava_warmup_history"
    return ".ava_warmup_history"


def load_app_config() -> AppConfig:
    """Load warm-up configuration from environment variables."""

    return AppConfig(
        gc_region=os.getenv("AVA_WARMUP_REGION") or os.getenv("GC_REGION"),
        gc_deployment_id=(
            os.getenv("AVA_WARMUP_DEPLOYMENT_ID") or os.getenv("GC_DEPLOYMENT_ID")
        ),
        response_timeout=_env_int("AVA_WARMUP_RESPONSE_TIMEOUT", 90),
        success_threshold=_env_float("AVA_WARMUP_SUCCESS_THRESHOLD", 0.8),
        performance_diagnostics_enabled=_env_bool(
            "AVA_WARMUP_PERFORMANCE_DIAGNOSTICS_ENABLED",
            True,
        ),
        debug_capture_frames=_env_bool("AVA_WARMUP_DEBUG_CAPTURE_FRAMES", False),
        debug_capture_frame_limit=_env_int("AVA_WARMUP_DEBUG_FRAME_LIMIT", 8),
        history_dir=_default_history_dir(),
        history_max_runs=_env_int("AVA_WARMUP_HISTORY_MAX_RUNS", 50),
        history_full_json_runs=_env_int("AVA_WARMUP_HISTORY_FULL_JSON_RUNS", 20),
        history_gzip_runs=_env_int("AVA_WARMUP_HISTORY_GZIP_RUNS", 20),
        default_attempt_count=_env_int("AVA_WARMUP_DEFAULT_ATTEMPT_COUNT", 228),
        default_execution_mode=_env_str("AVA_WARMUP_DEFAULT_EXECUTION_MODE", "serial"),
        default_worker_count=_env_int("AVA_WARMUP_DEFAULT_WORKER_COUNT", 1),
        default_pacing_seconds=_env_float("AVA_WARMUP_DEFAULT_PACING_SECONDS", 1.0),
        default_performance_profile=_env_str(
            "AVA_WARMUP_DEFAULT_PERFORMANCE_PROFILE", "safe_adaptive"
        ),
        default_cadence=_env_str("AVA_WARMUP_DEFAULT_CADENCE", "hourly"),
        default_minute=_env_int("AVA_WARMUP_DEFAULT_MINUTE", 0),
        default_time_hhmm=_env_str("AVA_WARMUP_DEFAULT_TIME_HHMM", "02:00"),
        default_weekday=_env_int("AVA_WARMUP_DEFAULT_WEEKDAY", 0),
        default_day_of_month=_env_int("AVA_WARMUP_DEFAULT_DAY_OF_MONTH", 1),
        default_timezone=_env_str("AVA_WARMUP_DEFAULT_TIMEZONE", "UTC"),
        default_schedule_start_date=_env_optional_str("AVA_WARMUP_DEFAULT_SCHEDULE_START_DATE"),
        default_schedule_end_date=_env_optional_str("AVA_WARMUP_DEFAULT_SCHEDULE_END_DATE"),
        auto_schedule_enabled=_env_bool("AVA_WARMUP_AUTO_SCHEDULE_ENABLED", False),
        server_host=_env_str("HOST", _env_str("AVA_WARMUP_HOST", "0.0.0.0")),
        server_port=_env_int("PORT", _env_int("AVA_WARMUP_PORT", 8080)),
        admin_user=_env_optional_str("ADMIN_USER"),
        admin_password=_env_optional_str("ADMIN_PASSWORD"),
        session_secret_key=_env_optional_str("SESSION_SECRET_KEY"),
    )


def merge_config(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """Return a copy of config with non-None overrides applied."""

    clean = {key: value for key, value in overrides.items() if value is not None}
    return config.model_copy(update=clean)
