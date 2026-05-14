"""AVA Spec Warm Up runner for transport-only Web Messaging checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
from typing import Any, Optional

from .diagnostics import build_performance_diagnostics
from .progress import ProgressEmitter
from .schemas import (
    AppConfig,
    AttemptResult,
    Message,
    MessageRole,
    ModelWarmupRunMetadata,
    ProgressEvent,
    ProgressEventType,
    ScenarioResult,
    TestReport,
    TimeoutDiagnostics,
)
from .suites import (
    DEFAULT_WARMUP_MESSAGE,
    DEFAULT_WARMUP_SCENARIO_NAME,
    DEFAULT_WARMUP_SUITE,
    DEFAULT_WARMUP_SUITE_NAME,
    WarmupSuiteSpec,
)
from .web_messaging_client import WebMessagingClient, WebMessagingError

MODEL_WARMUP_SUITE_NAME = DEFAULT_WARMUP_SUITE_NAME
MODEL_WARMUP_SCENARIO_NAME = DEFAULT_WARMUP_SCENARIO_NAME
MODEL_WARMUP_FIXED_MESSAGE = DEFAULT_WARMUP_MESSAGE
MODEL_WARMUP_DEFAULT_ATTEMPTS = 228
MODEL_WARMUP_PACING_CHOICES = {0.5, 1.0, 2.5, 5.0, 7.5}
MODEL_WARMUP_PERFORMANCE_PROFILE_SAFE_ADAPTIVE = "safe_adaptive"
MODEL_WARMUP_ADAPTIVE_WINDOW = 20
MODEL_WARMUP_HIGH_PRESSURE_RATE = 0.10
MODEL_WARMUP_CRITICAL_PRESSURE_RATE = 0.20
MODEL_WARMUP_HEALTHY_RATE = 0.03


@dataclass(frozen=True)
class ModelWarmUpRunRequest:
    """Operator-selected inputs for an AVA Spec Warm Up run."""

    deployment_id: str
    region: str
    recorded_model: Optional[str] = None
    execution_mode: str = "serial"
    worker_count: int = 1
    pacing_seconds: float = 1.0
    performance_profile: str = MODEL_WARMUP_PERFORMANCE_PROFILE_SAFE_ADAPTIVE
    attempt_count: int = MODEL_WARMUP_DEFAULT_ATTEMPTS
    trigger_source: str = "manual"
    schedule_id: Optional[str] = None
    scheduled_fire_at_utc: Optional[datetime] = None
    schedule_cadence: Optional[str] = None
    schedule_label: Optional[str] = None
    suite_spec: WarmupSuiteSpec = DEFAULT_WARMUP_SUITE


def normalize_model_warmup_execution_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"serial", "parallel"}:
        raise ValueError("AVA Spec Warm Up execution mode must be serial or parallel.")
    return normalized


def normalize_model_warmup_workers(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("AVA Spec Warm Up parallel workers must be a number.") from None
    return max(1, min(parsed, 10))


def normalize_model_warmup_attempt_count(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("AVA Spec Warm Up attempt count must be a number.") from None
    if parsed < 1:
        raise ValueError("AVA Spec Warm Up attempt count must be at least 1.")
    return parsed


def normalize_model_warmup_pacing(value: float | str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            "AVA Spec Warm Up pacing must be 0.5, 1.0, 2.5, 5.0, or 7.5 seconds."
        ) from None
    if parsed not in MODEL_WARMUP_PACING_CHOICES:
        raise ValueError(
            "AVA Spec Warm Up pacing must be 0.5, 1.0, 2.5, 5.0, or 7.5 seconds."
        )
    return parsed


def normalize_model_warmup_performance_profile(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return MODEL_WARMUP_PERFORMANCE_PROFILE_SAFE_ADAPTIVE
    if normalized != MODEL_WARMUP_PERFORMANCE_PROFILE_SAFE_ADAPTIVE:
        raise ValueError("AVA Spec Warm Up performance profile must be safe_adaptive.")
    return normalized


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(float(value) for value in values)

    def percentile(rank: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * rank
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

    return {
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "p99": round(percentile(0.99), 3),
    }


def build_model_warmup_metadata(
    request: ModelWarmUpRunRequest,
    *,
    completed_attempts: int = 0,
    effective_worker_count: Optional[int] = None,
    effective_pacing_seconds: Optional[float] = None,
    attempts_per_second: Optional[float] = None,
    duration_percentiles: Optional[dict[str, float]] = None,
    stage_duration_percentiles: Optional[dict[str, dict[str, float]]] = None,
    adaptive_adjustments: Optional[list[dict[str, Any]]] = None,
) -> ModelWarmupRunMetadata:
    """Create report metadata for an AVA Spec Warm Up run."""

    execution_mode = normalize_model_warmup_execution_mode(request.execution_mode)
    worker_count = 1
    if execution_mode == "parallel":
        worker_count = normalize_model_warmup_workers(request.worker_count)
    pacing_seconds = normalize_model_warmup_pacing(request.pacing_seconds)
    return ModelWarmupRunMetadata(
        deployment_id=request.deployment_id,
        region=request.region,
        recorded_model=request.recorded_model,
        execution_mode=execution_mode,
        worker_count=worker_count,
        pacing_seconds=pacing_seconds,
        performance_profile=normalize_model_warmup_performance_profile(
            request.performance_profile
        ),
        effective_worker_count=effective_worker_count or worker_count,
        effective_pacing_seconds=(
            effective_pacing_seconds
            if effective_pacing_seconds is not None
            else pacing_seconds
        ),
        attempts_per_second=attempts_per_second,
        duration_percentiles=duration_percentiles or {},
        stage_duration_percentiles=stage_duration_percentiles or {},
        adaptive_adjustments=adaptive_adjustments or [],
        suite_name=request.suite_spec.suite_name,
        scenario_name=request.suite_spec.scenario_name,
        fixed_message=request.suite_spec.first_message,
        warmup_messages=list(request.suite_spec.messages),
        planned_attempts=normalize_model_warmup_attempt_count(request.attempt_count),
        completed_attempts=max(0, int(completed_attempts)),
        trigger_source=request.trigger_source,
        schedule_id=request.schedule_id,
        scheduled_fire_at_utc=request.scheduled_fire_at_utc,
        schedule_cadence=request.schedule_cadence,
        schedule_label=request.schedule_label,
    )


class ModelWarmUpRunner:
    """Run Web Messaging transport attempts without Judge LLM evaluation."""

    def __init__(
        self,
        *,
        config: AppConfig,
        progress_emitter: ProgressEmitter,
        stop_event: Optional[Event] = None,
    ) -> None:
        self.config = config
        self.progress_emitter = progress_emitter
        self.stop_event = stop_event

    def _stop_requested(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _build_origin_from_region(self, region: str) -> str:
        normalized = (region or "").strip().lower()
        if normalized.startswith("https://"):
            normalized = normalized[len("https://") :]
        elif normalized.startswith("http://"):
            normalized = normalized[len("http://") :]
        normalized = normalized.split("/", 1)[0]
        if normalized.startswith("apps."):
            return f"https://{normalized}"
        if normalized.startswith("webmessaging."):
            normalized = normalized[len("webmessaging.") :]
        if not normalized:
            normalized = "mypurecloud.com"
        return f"https://apps.{normalized}"

    def _step_log_entry(
        self,
        step_log: list[dict],
        *,
        stage: str,
        message: str,
        duration_ms: Optional[float] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message": message,
        }
        if duration_ms is not None:
            entry["duration_ms"] = max(0.0, float(duration_ms))
        step_log.append(entry)

    async def _run_step(
        self,
        *,
        step_log: list[dict],
        stage_durations_ms: dict[str, float],
        status_callback,
        stage_key: str,
        start_stage: str,
        complete_stage: str,
        message: str,
        awaitable,
    ):
        if self._stop_requested():
            raise asyncio.CancelledError("AVA Spec Warm Up stop requested")
        if status_callback is not None:
            status_callback(message)
        started = time.monotonic()
        self._step_log_entry(step_log, stage=start_stage, message=message)
        try:
            result = await awaitable
        except Exception:
            stage_durations_ms[stage_key] = max(0.0, (time.monotonic() - started) * 1000)
            self._step_log_entry(
                step_log,
                stage=f"{start_stage.rsplit('_', 1)[0]}_error",
                message=f"{message} failed",
                duration_ms=stage_durations_ms[stage_key],
            )
            raise
        stage_durations_ms[stage_key] = max(0.0, (time.monotonic() - started) * 1000)
        self._step_log_entry(
            step_log,
            stage=complete_stage,
            message=f"{message} complete",
            duration_ms=stage_durations_ms[stage_key],
        )
        return result

    async def _run_attempt(
        self,
        request: ModelWarmUpRunRequest,
        *,
        attempt_number: int,
        status_callback,
    ) -> AttemptResult:
        conversation: list[Message] = []
        step_log: list[dict] = []
        stage_durations_ms: dict[str, float] = {}
        started_at = datetime.now(timezone.utc)
        attempt_started = time.monotonic()
        last_step_name: Optional[str] = None
        client = WebMessagingClient(
            region=request.region,
            deployment_id=request.deployment_id,
            timeout=self.config.response_timeout,
            origin=self._build_origin_from_region(request.region),
            debug_capture_frames=self.config.debug_capture_frames,
            debug_capture_frame_limit=self.config.debug_capture_frame_limit,
        )

        def build_result(
            *,
            success: bool,
            explanation: str,
            error: Optional[str] = None,
            timed_out: bool = False,
            skipped: bool = False,
            timeout_class: Optional[str] = None,
        ) -> AttemptResult:
            duration_seconds = time.monotonic() - attempt_started
            timeout_diagnostics = None
            if timed_out:
                timeout_diagnostics = TimeoutDiagnostics(
                    timeout_class=timeout_class or "model_warm_up_timeout",
                    step_name=last_step_name,
                    configured_timeout_seconds=float(self.config.response_timeout),
                    elapsed_attempt_seconds=duration_seconds,
                    conversation_total_messages=len(conversation),
                    conversation_user_messages=sum(
                        1 for message in conversation if message.role == MessageRole.USER
                    ),
                    conversation_agent_messages=sum(
                        1 for message in conversation if message.role == MessageRole.AGENT
                    ),
                    conversation_id=getattr(client, "conversation_id", None),
                    participant_id=getattr(client, "participant_id", None),
                    conversation_id_candidates=(
                        client.get_conversation_id_candidates()
                        if hasattr(client, "get_conversation_id_candidates")
                        else []
                    ),
                )
            return AttemptResult(
                attempt_number=attempt_number,
                success=success,
                conversation=conversation,
                explanation=explanation,
                error=error,
                timed_out=timed_out,
                skipped=skipped,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_seconds,
                step_log=[] if success else step_log,
                warmup_stage_durations_ms=stage_durations_ms,
                timeout_diagnostics=timeout_diagnostics,
                debug_frames=client.get_debug_frames() if hasattr(client, "get_debug_frames") else [],
                conversation_id=getattr(client, "conversation_id", None),
                participant_id=getattr(client, "participant_id", None),
                session_token=getattr(client, "_token", None),
                conversation_id_candidates=(
                    client.get_conversation_id_candidates()
                    if hasattr(client, "get_conversation_id_candidates")
                    else []
                ),
            )

        async def run_recorded_step(stage_prefix: str, message: str, awaitable):
            nonlocal last_step_name
            last_step_name = message
            return await self._run_step(
                step_log=step_log,
                stage_durations_ms=stage_durations_ms,
                status_callback=status_callback,
                stage_key=stage_prefix,
                start_stage=f"{stage_prefix}_start",
                complete_stage=f"{stage_prefix}_complete",
                message=message,
                awaitable=awaitable,
            )

        result_payload = {
            "success": False,
            "explanation": "AVA Spec Warm Up attempt failed before completion.",
            "error": None,
            "timed_out": False,
            "skipped": False,
        }
        try:
            await run_recorded_step("connect", "Connecting to Web Messaging", client.connect())
            await run_recorded_step("join", "Sending join event", client.send_join())
            welcome = await run_recorded_step(
                "welcome_wait",
                "Waiting for welcome message",
                client.wait_for_welcome(),
            )
            conversation.append(
                Message(role=MessageRole.AGENT, content=welcome, timestamp=datetime.now(timezone.utc))
            )
            for message_index, warmup_message in enumerate(request.suite_spec.messages, start=1):
                conversation.append(
                    Message(
                        role=MessageRole.USER,
                        content=warmup_message,
                        timestamp=datetime.now(timezone.utc),
                    )
                )
                stage_suffix = "" if message_index == 1 else f"_{message_index}"
                await run_recorded_step(
                    f"message_send{stage_suffix}",
                    f"Sending warm-up message {message_index}: {warmup_message}",
                    client.send_message(warmup_message),
                )
                agent_response = await run_recorded_step(
                    f"agent_response_wait{stage_suffix}",
                    f"Waiting for agent response {message_index}",
                    client.receive_response(),
                )
                conversation.append(
                    Message(
                        role=MessageRole.AGENT,
                        content=agent_response,
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            result_payload = {
                "success": True,
                "explanation": "AVA Spec Warm Up completed; no judgement performed.",
            }
        except asyncio.CancelledError as exc:
            result_payload = {
                "success": False,
                "explanation": "AVA Spec Warm Up attempt stopped before completion.",
                "error": str(exc),
                "skipped": True,
            }
        except TimeoutError as exc:
            result_payload = {
                "success": False,
                "explanation": "AVA Spec Warm Up attempt timed out; no judgement performed.",
                "error": str(exc),
                "timed_out": True,
            }
        except WebMessagingError as exc:
            result_payload = {
                "success": False,
                "explanation": (
                    "AVA Spec Warm Up attempt failed due to Web Messaging error; "
                    "no judgement performed."
                ),
                "error": str(exc),
            }
        except Exception as exc:
            result_payload = {
                "success": False,
                "explanation": "AVA Spec Warm Up attempt failed; no judgement performed.",
                "error": str(exc),
            }
        finally:
            disconnect_started = time.monotonic()
            self._step_log_entry(
                step_log,
                stage="disconnect_start",
                message="Disconnecting from Web Messaging",
            )
            try:
                await client.disconnect()
            finally:
                stage_durations_ms["disconnect"] = max(
                    0.0,
                    (time.monotonic() - disconnect_started) * 1000,
                )
                self._step_log_entry(
                    step_log,
                    stage="disconnect_complete",
                    message="Disconnect complete",
                    duration_ms=stage_durations_ms["disconnect"],
                )
        return build_result(**result_payload)

    async def run(self, request: ModelWarmUpRunRequest) -> TestReport:
        """Execute the fixed AVA Spec Warm Up suite."""

        started = time.monotonic()
        execution_mode = normalize_model_warmup_execution_mode(request.execution_mode)
        worker_count = (
            normalize_model_warmup_workers(request.worker_count)
            if execution_mode == "parallel"
            else 1
        )
        pacing_seconds = normalize_model_warmup_pacing(request.pacing_seconds)
        performance_profile = normalize_model_warmup_performance_profile(
            request.performance_profile
        )
        suite = request.suite_spec
        planned_attempts = normalize_model_warmup_attempt_count(request.attempt_count)
        active_worker_limit = worker_count
        effective_pacing_seconds = pacing_seconds
        healthy_windows = 0
        window_pressure_signals: list[bool] = []
        adaptive_adjustments: list[dict[str, Any]] = []
        completed_attempts = 0
        successes = 0
        timeouts = 0
        skipped = 0
        attempts: list[AttemptResult] = []
        attempt_queue: asyncio.Queue[int] = asyncio.Queue()
        for attempt_number in range(1, planned_attempts + 1):
            attempt_queue.put_nowait(attempt_number)
        event_lock = asyncio.Lock()

        self.progress_emitter.emit(
            ProgressEvent(
                event_type=ProgressEventType.SUITE_STARTED,
                suite_name=suite.suite_name,
                message=f"Starting AVA Spec Warm Up suite: {suite.suite_name}",
                planned_attempts=planned_attempts,
                completed_attempts=completed_attempts,
            )
        )
        self.progress_emitter.emit(
            ProgressEvent(
                event_type=ProgressEventType.SCENARIO_STARTED,
                suite_name=suite.suite_name,
                scenario_name=suite.scenario_name,
                message=(
                    f"Starting scenario: {suite.scenario_name} "
                    f"({planned_attempts} attempts)"
                ),
                planned_attempts=planned_attempts,
                completed_attempts=completed_attempts,
            )
        )
        self.progress_emitter.emit(
            ProgressEvent(
                event_type=ProgressEventType.ATTEMPT_STATUS,
                suite_name=suite.suite_name,
                scenario_name=suite.scenario_name,
                message=(
                    "AVA Spec Warm Up configured: "
                    f"mode={execution_mode}, workers={worker_count}, "
                    f"pacing={pacing_seconds:.1f}s, "
                    f"profile={performance_profile}, "
                    f"model={request.recorded_model or 'not recorded'}, "
                    f"suite={suite.suite_name}"
                ),
                planned_attempts=planned_attempts,
                completed_attempts=completed_attempts,
            )
        )

        def emit_summary_status(message: str) -> None:
            self.progress_emitter.emit(
                ProgressEvent(
                    event_type=ProgressEventType.ATTEMPT_STATUS,
                    suite_name=suite.suite_name,
                    scenario_name=suite.scenario_name,
                    message=message,
                    planned_attempts=planned_attempts,
                    completed_attempts=completed_attempts,
                )
            )

        def pressure_signal(result: AttemptResult) -> bool:
            if result.skipped:
                return False
            return bool(result.timed_out or (not result.success and result.error))

        def maybe_apply_adaptive_backpressure() -> None:
            nonlocal active_worker_limit, effective_pacing_seconds, healthy_windows
            if performance_profile != MODEL_WARMUP_PERFORMANCE_PROFILE_SAFE_ADAPTIVE:
                return
            if len(window_pressure_signals) < MODEL_WARMUP_ADAPTIVE_WINDOW:
                return

            window_size = len(window_pressure_signals)
            signal_count = sum(1 for signal in window_pressure_signals if signal)
            signal_rate = signal_count / window_size if window_size else 0.0
            window_pressure_signals.clear()

            from_workers = active_worker_limit
            from_pacing = effective_pacing_seconds
            reason: Optional[str] = None

            if signal_rate > MODEL_WARMUP_HIGH_PRESSURE_RATE:
                healthy_windows = 0
                active_worker_limit = max(1, active_worker_limit - 1)
                reason = "high_error_pressure"
                if signal_rate > MODEL_WARMUP_CRITICAL_PRESSURE_RATE:
                    effective_pacing_seconds = min(7.5, effective_pacing_seconds + 1.0)
                    reason = "critical_error_pressure"
            elif signal_rate < MODEL_WARMUP_HEALTHY_RATE:
                healthy_windows += 1
                if healthy_windows >= 2:
                    restored = False
                    if active_worker_limit < worker_count:
                        active_worker_limit += 1
                        restored = True
                    if effective_pacing_seconds > pacing_seconds:
                        effective_pacing_seconds = max(
                            pacing_seconds,
                            effective_pacing_seconds - 1.0,
                        )
                        restored = True
                    if restored:
                        reason = "healthy_recovery"
                    healthy_windows = 0
            else:
                healthy_windows = 0

            if reason and (
                active_worker_limit != from_workers
                or effective_pacing_seconds != from_pacing
            ):
                adjustment = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "completed_attempts": completed_attempts,
                    "from_worker_count": from_workers,
                    "to_worker_count": active_worker_limit,
                    "from_pacing_seconds": round(from_pacing, 3),
                    "to_pacing_seconds": round(effective_pacing_seconds, 3),
                    "window_attempts": window_size,
                    "window_error_count": signal_count,
                    "window_error_rate": round(signal_rate, 4),
                    "reason": reason,
                }
                adaptive_adjustments.append(adjustment)
                emit_summary_status(
                    "AVA Spec Warm Up adaptive backpressure: "
                    f"{reason}; workers {from_workers}->{active_worker_limit}, "
                    f"pacing {from_pacing:.1f}s->{effective_pacing_seconds:.1f}s, "
                    f"window error rate {signal_rate:.1%}"
                )

        async def worker(worker_index: int) -> None:
            nonlocal completed_attempts, successes, timeouts, skipped
            last_start_monotonic: Optional[float] = None
            while not self._stop_requested():
                while worker_index > active_worker_limit:
                    if self._stop_requested() or attempt_queue.empty():
                        return
                    await asyncio.sleep(0.2)
                try:
                    attempt_number = attempt_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if last_start_monotonic is not None:
                    remaining = max(
                        0.0,
                        effective_pacing_seconds - (time.monotonic() - last_start_monotonic),
                    )
                    while remaining > 0:
                        if self._stop_requested():
                            return
                        tick = min(0.2, remaining)
                        await asyncio.sleep(tick)
                        remaining -= tick
                if self._stop_requested():
                    return
                last_start_monotonic = time.monotonic()

                async with event_lock:
                    self.progress_emitter.emit(
                        ProgressEvent(
                            event_type=ProgressEventType.ATTEMPT_STARTED,
                            suite_name=suite.suite_name,
                            scenario_name=suite.scenario_name,
                            attempt_number=attempt_number,
                            message=f"Attempt {attempt_number} started",
                            planned_attempts=planned_attempts,
                            completed_attempts=completed_attempts,
                        )
                    )

                result = await self._run_attempt(
                    request,
                    attempt_number=attempt_number,
                    status_callback=None,
                )

                async with event_lock:
                    attempts.append(result)
                    completed_attempts += 1
                    if result.success:
                        successes += 1
                    if result.timed_out:
                        timeouts += 1
                    if result.skipped:
                        skipped += 1
                    window_pressure_signals.append(pressure_signal(result))
                    maybe_apply_adaptive_backpressure()
                    duration = time.monotonic() - started
                    throughput = completed_attempts / duration if duration > 0 else 0.0
                    self.progress_emitter.emit(
                        ProgressEvent(
                            event_type=ProgressEventType.ATTEMPT_COMPLETED,
                            suite_name=suite.suite_name,
                            scenario_name=suite.scenario_name,
                            attempt_number=result.attempt_number,
                            success=result.success,
                            message=(
                                f"Attempt {result.attempt_number}: "
                                f"{'success' if result.success else 'failure'} "
                                f"({completed_attempts}/{planned_attempts}) | "
                                f"{throughput:.2f} attempts/sec | "
                                f"active workers={active_worker_limit} | "
                                f"pacing={effective_pacing_seconds:.1f}s"
                            ),
                            attempt_result=result,
                            planned_attempts=planned_attempts,
                            completed_attempts=completed_attempts,
                        )
                    )

        workers = [asyncio.create_task(worker(index + 1)) for index in range(worker_count)]
        await asyncio.gather(*workers)

        attempts.sort(key=lambda attempt: attempt.attempt_number)
        failures = max(0, len(attempts) - successes - timeouts - skipped)
        success_rate = successes / len(attempts) if attempts else 0.0
        duration = time.monotonic() - started
        attempts_per_second = len(attempts) / duration if duration > 0 else 0.0
        duration_values = [
            float(attempt.duration_seconds)
            for attempt in attempts
            if attempt.duration_seconds is not None
        ]
        stage_values: dict[str, list[float]] = {}
        for attempt in attempts:
            for stage, duration_ms in attempt.warmup_stage_durations_ms.items():
                stage_values.setdefault(stage, []).append(float(duration_ms))
        stage_duration_percentiles = {
            stage: _percentiles(values) for stage, values in sorted(stage_values.items())
        }
        scenario = ScenarioResult(
            scenario_name=suite.scenario_name,
            attempts=len(attempts),
            successes=successes,
            failures=failures,
            timeouts=timeouts,
            skipped=skipped,
            success_rate=success_rate,
            is_regression=success_rate < self.config.success_threshold if attempts else False,
            attempt_results=attempts,
        )
        report = TestReport(
            suite_name=suite.suite_name,
            timestamp=datetime.now(timezone.utc),
            duration_seconds=duration,
            scenario_results=[scenario] if attempts else [],
            overall_attempts=len(attempts),
            overall_successes=successes,
            overall_failures=failures,
            overall_timeouts=timeouts,
            overall_skipped=skipped,
            overall_success_rate=success_rate,
            model_warmup_run=build_model_warmup_metadata(
                request,
                completed_attempts=len(attempts),
                effective_worker_count=active_worker_limit,
                effective_pacing_seconds=effective_pacing_seconds,
                attempts_per_second=round(attempts_per_second, 4),
                duration_percentiles=_percentiles(duration_values),
                stage_duration_percentiles=stage_duration_percentiles,
                adaptive_adjustments=adaptive_adjustments,
            ),
            stopped_by_user=self._stop_requested(),
            stop_mode="immediate" if self._stop_requested() else None,
            has_regressions=scenario.is_regression if attempts else False,
            regression_threshold=self.config.success_threshold,
        )
        if self.config.performance_diagnostics_enabled:
            report.performance_diagnostics = build_performance_diagnostics(
                report,
                run_type="model_warm_up",
                planned_attempts=planned_attempts,
                worker_count=active_worker_limit,
                pacing_seconds=effective_pacing_seconds,
                notes=[
                    f"AVA Spec Warm Up performance profile: {performance_profile}",
                    "Successful warm-up attempts retain compact stage metrics only.",
                ],
            )

        self.progress_emitter.emit(
            ProgressEvent(
                event_type=ProgressEventType.SCENARIO_COMPLETED,
                suite_name=suite.suite_name,
                scenario_name=suite.scenario_name,
                success_rate=success_rate,
                message=(
                    f"Scenario completed: {suite.scenario_name} - "
                    f"{success_rate:.0%} completion rate"
                ),
                planned_attempts=planned_attempts,
                completed_attempts=completed_attempts,
            )
        )
        completed_message = (
            f"AVA Spec Warm Up completed: {suite.suite_name} in {duration:.1f}s"
        )
        if self._stop_requested():
            completed_message = (
                f"AVA Spec Warm Up stopped early: {suite.suite_name} "
                f"after {duration:.1f}s"
            )
        self.progress_emitter.emit(
            ProgressEvent(
                event_type=ProgressEventType.SUITE_COMPLETED,
                suite_name=suite.suite_name,
                message=completed_message,
                duration_seconds=duration,
                planned_attempts=planned_attempts,
                completed_attempts=completed_attempts,
            )
        )
        return report
