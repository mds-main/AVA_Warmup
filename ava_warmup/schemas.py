"""Pydantic schemas for the standalone AVA Spec Warm Up app."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class AppConfig(BaseModel):
    """Runtime configuration used by the warm-up runner and web app."""

    gc_region: Optional[str] = None
    gc_deployment_id: Optional[str] = None
    response_timeout: int = 90
    success_threshold: float = 0.8
    performance_diagnostics_enabled: bool = True
    debug_capture_frames: bool = False
    debug_capture_frame_limit: int = 8
    history_dir: str = ".ava_warmup_history"
    history_max_runs: int = 50
    history_full_json_runs: int = 20
    history_gzip_runs: int = 20
    default_attempt_count: int = 228
    default_execution_mode: str = "serial"
    default_worker_count: int = 1
    default_pacing_seconds: float = 1.0
    default_performance_profile: str = "safe_adaptive"
    default_cadence: str = "hourly"
    default_minute: int = 0
    default_time_hhmm: str = "02:00"
    default_weekday: int = 0
    default_day_of_month: int = 1
    default_timezone: str = "UTC"
    default_schedule_start_date: Optional[str] = None
    default_schedule_end_date: Optional[str] = None
    auto_schedule_enabled: bool = False
    server_host: str = "0.0.0.0"
    server_port: int = 8080
    admin_user: Optional[str] = None
    admin_password: Optional[str] = None
    session_secret_key: Optional[str] = None

    @field_validator(
        "response_timeout",
        "history_max_runs",
        "history_full_json_runs",
        "history_gzip_runs",
        "default_attempt_count",
        "server_port",
    )
    @classmethod
    def normalize_positive_int(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("success_threshold")
    @classmethod
    def normalize_success_threshold(cls, value: float) -> float:
        parsed = float(value)
        return max(0.0, min(parsed, 1.0))

    @field_validator("debug_capture_frame_limit")
    @classmethod
    def normalize_debug_frame_limit(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("default_worker_count")
    @classmethod
    def clamp_default_worker_count(cls, value: int) -> int:
        parsed = int(value)
        return max(1, min(parsed, 5))

    @field_validator("default_minute")
    @classmethod
    def clamp_default_minute(cls, value: int) -> int:
        parsed = int(value)
        return max(0, min(parsed, 59))

    @field_validator("default_weekday")
    @classmethod
    def clamp_default_weekday(cls, value: int) -> int:
        parsed = int(value)
        return max(0, min(parsed, 6))

    @field_validator("default_day_of_month")
    @classmethod
    def clamp_default_day_of_month(cls, value: int) -> int:
        parsed = int(value)
        return max(1, min(parsed, 31))


class MessageRole(str, Enum):
    """Role of a message sender in a warm-up conversation."""

    AGENT = "agent"
    USER = "user"


class Message(BaseModel):
    """A single Web Messaging transcript message."""

    role: MessageRole
    content: str
    timestamp: Optional[datetime] = None


class TimeoutDiagnostics(BaseModel):
    """Structured timeout telemetry captured for failed attempts."""

    timeout_class: str
    step_name: Optional[str] = None
    configured_timeout_seconds: Optional[float] = None
    elapsed_attempt_seconds: Optional[float] = None
    conversation_total_messages: int = 0
    conversation_user_messages: int = 0
    conversation_agent_messages: int = 0
    conversation_id: Optional[str] = None
    participant_id: Optional[str] = None
    conversation_id_candidates: list[str] = Field(default_factory=list)

    @field_validator("timeout_class")
    @classmethod
    def normalize_timeout_class(cls, value: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        if not normalized:
            raise ValueError("timeout_class must not be blank")
        return normalized

    @field_validator("conversation_id_candidates", mode="before")
    @classmethod
    def normalize_conversation_id_candidates(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            deduped: list[str] = []
            seen: set[str] = set()
            for item in value:
                text = str(item or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                deduped.append(text)
            return deduped
        return []


class AttemptResult(BaseModel):
    """Result of one transport-only Web Messaging attempt."""

    attempt_number: int
    success: bool
    conversation: list[Message]
    explanation: str
    error: Optional[str] = None
    timed_out: bool = False
    skipped: bool = False
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    step_log: list[dict[str, Any]] = Field(default_factory=list)
    warmup_stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    judge_diagnostics: list[Any] = Field(default_factory=list)
    debug_frames: list[dict[str, Any]] = Field(default_factory=list)
    timeout_diagnostics: Optional[TimeoutDiagnostics] = None


class PerformanceStageSummary(BaseModel):
    """Compact timing summary for one performance stage."""

    stage: str
    count: int = 0
    total_ms: float = 0.0
    average_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0

    @field_validator("stage")
    @classmethod
    def normalize_stage(cls, value: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        if not normalized:
            raise ValueError("performance stage must not be blank")
        return normalized


class PerformanceDiagnostics(BaseModel):
    """Optional run-level performance diagnostics for bottleneck analysis."""

    enabled: bool = True
    run_type: str = "model_warm_up"
    planned_attempts: int = 0
    completed_attempts: int = 0
    duration_seconds: float = 0.0
    attempts_per_second: float = 0.0
    worker_count: Optional[int] = None
    pacing_seconds: Optional[float] = None
    timeout_error_rate: float = 0.0
    timeout_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    stage_summaries: list[PerformanceStageSummary] = Field(default_factory=list)
    judge_operation_summaries: list[PerformanceStageSummary] = Field(default_factory=list)
    slowest_stages: list[PerformanceStageSummary] = Field(default_factory=list)
    adaptive_pacing_summary: Optional[dict[str, Any]] = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("run_type")
    @classmethod
    def normalize_run_type(cls, value: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        return normalized or "model_warm_up"


class ModelWarmupRunMetadata(BaseModel):
    """Run-level metadata for AVA Spec Warm Up transport checks."""

    enabled: bool = True
    deployment_id: str
    region: str
    recorded_model: Optional[str] = None
    execution_mode: str = "serial"
    worker_count: int = 1
    pacing_seconds: float = 1.0
    performance_profile: str = "safe_adaptive"
    effective_worker_count: int = 1
    effective_pacing_seconds: float = 1.0
    attempts_per_second: Optional[float] = None
    duration_percentiles: dict[str, float] = Field(default_factory=dict)
    stage_duration_percentiles: dict[str, dict[str, float]] = Field(default_factory=dict)
    adaptive_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    suite_name: str = "AVA Spec Warm Up Suite"
    scenario_name: str = "No Help Needed Warm Up"
    fixed_message: str = "no help needed"
    warmup_messages: list[str] = Field(default_factory=lambda: ["no help needed"])
    planned_attempts: int = 228
    completed_attempts: int = 0
    trigger_source: str = "manual"
    schedule_id: Optional[str] = None
    scheduled_fire_at_utc: Optional[datetime] = None
    schedule_cadence: Optional[str] = None
    schedule_label: Optional[str] = None

    @field_validator("deployment_id", "region")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("AVA Spec Warm Up required text fields must not be blank")
        return normalized

    @field_validator("recorded_model", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("trigger_source")
    @classmethod
    def normalize_trigger_source(cls, value: str) -> str:
        normalized = str(value or "manual").strip().lower()
        return normalized if normalized in {"manual", "scheduled"} else "manual"

    @field_validator("execution_mode")
    @classmethod
    def normalize_execution_mode(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"serial", "parallel"}:
            raise ValueError("AVA Spec Warm Up execution_mode must be serial or parallel")
        return normalized

    @field_validator("performance_profile")
    @classmethod
    def normalize_performance_profile(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized != "safe_adaptive":
            raise ValueError("AVA Spec Warm Up performance_profile must be safe_adaptive")
        return normalized

    @field_validator("worker_count", "effective_worker_count")
    @classmethod
    def clamp_worker_count(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 1:
            return 1
        if parsed > 5:
            return 5
        return parsed

    @field_validator("pacing_seconds")
    @classmethod
    def normalize_pacing_seconds(cls, value: float) -> float:
        parsed = float(value)
        if parsed not in {0.5, 1.0, 2.5, 5.0, 7.5}:
            raise ValueError(
                "AVA Spec Warm Up pacing_seconds must be 0.5, 1.0, 2.5, 5.0, or 7.5"
            )
        return parsed

    @field_validator("effective_pacing_seconds")
    @classmethod
    def normalize_effective_pacing_seconds(cls, value: float) -> float:
        parsed = float(value)
        return max(0.5, min(parsed, 7.5))

    @field_validator("planned_attempts")
    @classmethod
    def normalize_planned_attempts(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 1:
            raise ValueError("AVA Spec Warm Up planned_attempts must be at least 1")
        return parsed


class ScenarioResult(BaseModel):
    """Result of running all attempts for the fixed warm-up scenario."""

    scenario_name: str
    attempts: int
    successes: int
    failures: int
    timeouts: int = 0
    skipped: int = 0
    success_rate: float
    is_regression: bool
    attempt_results: list[AttemptResult]


class TestReport(BaseModel):
    """Aggregated output of one AVA Spec Warm Up run."""

    suite_name: str
    timestamp: datetime
    duration_seconds: float
    scenario_results: list[ScenarioResult]
    overall_attempts: int
    overall_successes: int
    overall_failures: int
    overall_timeouts: int = 0
    overall_skipped: int = 0
    overall_success_rate: float
    model_warmup_run: Optional[ModelWarmupRunMetadata] = None
    performance_diagnostics: Optional[PerformanceDiagnostics] = None
    adaptive_attempt_pacing_enabled: bool = False
    adaptive_attempt_pacing_base_interval_seconds: Optional[float] = None
    adaptive_attempt_pacing_final_interval_seconds: Optional[float] = None
    adaptive_attempt_pacing_adjustment_count: int = 0
    adaptive_attempt_pacing_adjustments: list[Any] = Field(default_factory=list)
    stopped_by_user: bool = False
    stop_mode: Optional[str] = None
    stop_requested_at: Optional[datetime] = None
    stop_finalized_at: Optional[datetime] = None
    force_finalized: bool = False
    has_regressions: bool
    regression_threshold: float


class ProgressEventType(str, Enum):
    """Types of progress events emitted during warm-up execution."""

    SUITE_STARTED = "suite_started"
    SCENARIO_STARTED = "scenario_started"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_STATUS = "attempt_status"
    ATTEMPT_COMPLETED = "attempt_completed"
    SCENARIO_COMPLETED = "scenario_completed"
    SUITE_COMPLETED = "suite_completed"


class ProgressEvent(BaseModel):
    """A progress event emitted during a warm-up run."""

    event_type: ProgressEventType
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    suite_name: Optional[str] = None
    scenario_name: Optional[str] = None
    attempt_number: Optional[int] = None
    success: Optional[bool] = None
    success_rate: Optional[float] = None
    message: str
    duration_seconds: Optional[float] = None
    attempt_result: Optional[AttemptResult] = None
    planned_attempts: Optional[int] = None
    completed_attempts: Optional[int] = None
