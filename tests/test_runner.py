"""Tests for the transport-only AVA Spec Warm Up runner."""

import asyncio
from datetime import datetime, timezone

import pytest

from ava_warmup.runner import (
    MODEL_WARMUP_DEFAULT_ATTEMPTS,
    MODEL_WARMUP_FIXED_MESSAGE,
    ModelWarmUpRunRequest,
    ModelWarmUpRunner,
    build_model_warmup_metadata,
)
from ava_warmup.schemas import AppConfig, MessageRole
from ava_warmup.progress import ProgressEmitter
from ava_warmup.suites import WarmupSuiteSpec


class _FakeWebMessagingClient:
    active_connections = 0
    max_active_connections = 0
    welcome_outcomes = []
    sent_messages = []

    def __init__(self, *args, **kwargs):
        self.raise_timeout = kwargs.get("deployment_id") == "timeout"
        self.conversation_id = None
        self.participant_id = None

    async def connect(self):
        type(self).active_connections += 1
        type(self).max_active_connections = max(
            type(self).max_active_connections,
            type(self).active_connections,
        )
        await asyncio.sleep(0.01)

    async def send_join(self):
        await asyncio.sleep(0)

    async def wait_for_welcome(self):
        outcome = None
        if type(self).welcome_outcomes:
            outcome = type(self).welcome_outcomes.pop(0)
        if self.raise_timeout or outcome == "timeout":
            raise TimeoutError("welcome timed out")
        return "Welcome"

    async def send_message(self, text):
        self.sent_message = text
        type(self).sent_messages.append(text)
        await asyncio.sleep(0)

    async def receive_response(self):
        return "Goodbye"

    async def disconnect(self):
        type(self).active_connections = max(0, type(self).active_connections - 1)

    def get_debug_frames(self):
        return []

    def get_conversation_id_candidates(self):
        return []


@pytest.fixture(autouse=True)
def reset_fake_client():
    _FakeWebMessagingClient.active_connections = 0
    _FakeWebMessagingClient.max_active_connections = 0
    _FakeWebMessagingClient.welcome_outcomes = []
    _FakeWebMessagingClient.sent_messages = []


def _config() -> AppConfig:
    return AppConfig(
        gc_region="usw2.pure.cloud",
        gc_deployment_id="deploy-id",
        response_timeout=5,
        success_threshold=0.8,
    )


def test_model_warmup_metadata_uses_configurable_attempt_count():
    scheduled_fire_at = datetime(2026, 4, 27, 14, 0, tzinfo=timezone.utc)
    metadata = build_model_warmup_metadata(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            recorded_model="gemma4:e4b",
            execution_mode="parallel",
            worker_count=9,
            pacing_seconds=1.0,
            attempt_count=42,
            trigger_source="scheduled",
            schedule_id="schedule-123",
            scheduled_fire_at_utc=scheduled_fire_at,
            schedule_cadence="daily",
            schedule_label="Daily at 10:00 (America/New_York)",
        )
    )

    assert MODEL_WARMUP_DEFAULT_ATTEMPTS == 228
    assert metadata.planned_attempts == 42
    assert metadata.worker_count == 9
    assert metadata.performance_profile == "safe_adaptive"
    assert metadata.pacing_seconds == 1.0
    assert metadata.fixed_message == MODEL_WARMUP_FIXED_MESSAGE
    assert metadata.trigger_source == "scheduled"
    assert metadata.schedule_id == "schedule-123"
    assert metadata.scheduled_fire_at_utc == scheduled_fire_at
    assert metadata.schedule_cadence == "daily"
    assert metadata.schedule_label == "Daily at 10:00 (America/New_York)"


@pytest.mark.asyncio
async def test_model_warmup_success_records_conversation_and_compact_timings(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    runner = ModelWarmUpRunner(config=_config(), progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            recorded_model="gemma4:e4b",
            execution_mode="serial",
            worker_count=1,
            pacing_seconds=1.0,
            attempt_count=1,
        )
    )

    attempt = report.scenario_results[0].attempt_results[0]
    assert report.overall_attempts == 1
    assert report.overall_successes == 1
    assert report.model_warmup_run is not None
    assert report.model_warmup_run.recorded_model == "gemma4:e4b"
    assert [message.role for message in attempt.conversation] == [
        MessageRole.AGENT,
        MessageRole.USER,
        MessageRole.AGENT,
    ]
    assert attempt.conversation[1].content == "no help needed"
    assert attempt.judge_diagnostics == []
    assert attempt.step_log == []
    assert "connect" in attempt.warmup_stage_durations_ms
    assert "agent_response_wait" in attempt.warmup_stage_durations_ms
    assert "disconnect" in attempt.warmup_stage_durations_ms
    assert report.model_warmup_run.attempts_per_second is not None
    assert report.model_warmup_run.duration_percentiles["p50"] >= 0
    assert "connect" in report.model_warmup_run.stage_duration_percentiles
    assert report.performance_diagnostics is not None
    assert report.performance_diagnostics.run_type == "model_warm_up"
    assert report.performance_diagnostics.worker_count == 1
    assert report.performance_diagnostics.slowest_stages


@pytest.mark.asyncio
async def test_model_warmup_custom_suite_dispatches_ordered_messages(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    suite = WarmupSuiteSpec(
        suite_id="custom",
        suite_name="Custom Warm Up Suite",
        scenario_name="Custom Scenario",
        messages=("hello", "still there?"),
    )
    runner = ModelWarmUpRunner(config=_config(), progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            execution_mode="serial",
            worker_count=1,
            pacing_seconds=1.0,
            attempt_count=1,
            suite_spec=suite,
        )
    )

    attempt = report.scenario_results[0].attempt_results[0]
    assert _FakeWebMessagingClient.sent_messages == ["hello", "still there?"]
    assert report.suite_name == "Custom Warm Up Suite"
    assert report.scenario_results[0].scenario_name == "Custom Scenario"
    assert report.model_warmup_run.suite_name == "Custom Warm Up Suite"
    assert report.model_warmup_run.scenario_name == "Custom Scenario"
    assert report.model_warmup_run.fixed_message == "hello"
    assert report.model_warmup_run.warmup_messages == ["hello", "still there?"]
    assert [message.content for message in attempt.conversation if message.role == MessageRole.USER] == [
        "hello",
        "still there?",
    ]


@pytest.mark.asyncio
async def test_model_warmup_omits_performance_diagnostics_when_disabled(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    config = _config()
    config.performance_diagnostics_enabled = False
    runner = ModelWarmUpRunner(config=config, progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            execution_mode="serial",
            worker_count=1,
            pacing_seconds=1.0,
            attempt_count=1,
        )
    )

    assert report.overall_attempts == 1
    assert report.performance_diagnostics is None


@pytest.mark.asyncio
async def test_model_warmup_timeout_marks_attempt_timed_out(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    runner = ModelWarmUpRunner(config=_config(), progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="timeout",
            region="usw2.pure.cloud",
            execution_mode="serial",
            worker_count=1,
            pacing_seconds=1.0,
            attempt_count=1,
        )
    )

    attempt = report.scenario_results[0].attempt_results[0]
    assert report.overall_timeouts == 1
    assert attempt.timed_out is True
    assert attempt.step_log
    assert attempt.timeout_diagnostics is not None
    assert attempt.timeout_diagnostics.timeout_class == "model_warm_up_timeout"


@pytest.mark.asyncio
async def test_model_warmup_parallel_mode_uses_selected_workers(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    runner = ModelWarmUpRunner(config=_config(), progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            execution_mode="parallel",
            worker_count=5,
            pacing_seconds=1.0,
            attempt_count=5,
        )
    )

    assert report.overall_attempts == 5
    assert report.overall_successes == 5
    assert _FakeWebMessagingClient.max_active_connections == 5
    assert report.model_warmup_run is not None
    assert report.model_warmup_run.worker_count == 5


@pytest.mark.asyncio
async def test_model_warmup_adaptive_backpressure_reduces_and_recovers(monkeypatch):
    monkeypatch.setattr("ava_warmup.runner.MODEL_WARMUP_ADAPTIVE_WINDOW", 2)
    _FakeWebMessagingClient.welcome_outcomes = [
        "timeout",
        "timeout",
        "success",
        "success",
        "success",
        "success",
        "success",
        "success",
    ]
    monkeypatch.setattr("ava_warmup.runner.WebMessagingClient", _FakeWebMessagingClient)
    runner = ModelWarmUpRunner(config=_config(), progress_emitter=ProgressEmitter())

    report = await runner.run(
        ModelWarmUpRunRequest(
            deployment_id="deploy-id",
            region="usw2.pure.cloud",
            execution_mode="parallel",
            worker_count=2,
            pacing_seconds=1.0,
            attempt_count=8,
        )
    )

    assert report.model_warmup_run is not None
    adjustments = report.model_warmup_run.adaptive_adjustments
    assert any(item["reason"] == "critical_error_pressure" for item in adjustments)
    assert any(item["reason"] == "healthy_recovery" for item in adjustments)
    assert report.model_warmup_run.effective_worker_count == 2
    assert report.model_warmup_run.effective_pacing_seconds >= 1.0
