"""Flask web app for the standalone AVA Spec Warm Up workflow."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import secrets
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .config import load_app_config, merge_config
from .history import RunHistoryStore
from .progress import ProgressEmitter
from .runner import (
    MODEL_WARMUP_DEFAULT_ATTEMPTS,
    MODEL_WARMUP_FIXED_MESSAGE,
    MODEL_WARMUP_PACING_CHOICES,
    MODEL_WARMUP_SCENARIO_NAME,
    MODEL_WARMUP_SUITE_NAME,
    ModelWarmUpRunRequest,
    ModelWarmUpRunner,
    build_model_warmup_metadata,
    normalize_model_warmup_attempt_count,
    normalize_model_warmup_execution_mode,
    normalize_model_warmup_pacing,
    normalize_model_warmup_performance_profile,
    normalize_model_warmup_workers,
)
from .scheduler import (
    ModelWarmupScheduleStore,
    ModelWarmupScheduler,
    cadence_interval_hours,
    compute_next_model_warmup_run_utc,
    model_warmup_schedule_label,
    normalize_model_warmup_schedule_cadence,
    normalize_schedule_date_range,
    normalize_schedule_minute,
    normalize_schedule_month_day,
    normalize_schedule_weekday,
    parse_schedule_hhmm,
    validate_schedule_timezone_name,
)
from .schemas import AppConfig, ModelWarmupRunMetadata, ProgressEventType, TestReport
from .suites import (
    DEFAULT_WARMUP_SUITE,
    DEFAULT_WARMUP_SUITE_ID,
    WarmupSuiteSpec,
    load_available_suites,
    resolve_suite,
    suite_from_request_payload,
)


@dataclass
class ActiveRunControl:
    run_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    finalized: bool = False
    stop_requested_at: Optional[datetime] = None
    stop_finalized_at: Optional[datetime] = None
    force_finalized: bool = False


def create_app(config: Optional[AppConfig] = None) -> Flask:
    """Create the standalone warm-up Flask app."""

    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )

    base_config = config or load_app_config()
    if base_config.session_secret_key:
        app.secret_key = base_config.session_secret_key
    elif base_config.admin_password:
        app.secret_key = hashlib.sha256(
            f"ava-warmup-session::{base_config.admin_user or ''}::{base_config.admin_password}".encode("utf-8")
        ).hexdigest()
    else:
        app.secret_key = secrets.token_hex(32)
    history_store = RunHistoryStore(
        history_dir=base_config.history_dir,
        max_runs=base_config.history_max_runs,
        full_json_runs=base_config.history_full_json_runs,
        gzip_runs=base_config.history_gzip_runs,
    )
    schedule_store = ModelWarmupScheduleStore(history_dir=base_config.history_dir)
    app.config.update(
        app_config=base_config,
        history_store=history_store,
        model_warmup_schedule_store=schedule_store,
        model_warmup_schedule_status=schedule_store.load(),
        run_state_lock=threading.Lock(),
        run_active=False,
        stop_requested=False,
        stop_event=threading.Event(),
        active_run_control=None,
        active_run_id=None,
        active_run_type=None,
        active_trigger_source=None,
        active_model_warmup_metadata=None,
        scheduled_run_started_at_utc=None,
        progress_emitter=ProgressEmitter(),
        latest_report=None,
        latest_run_history_entry=None,
        last_run_config=base_config,
        model_warmup_scheduler=None,
    )

    def _wants_json() -> bool:
        return request.is_json or "application/json" in request.headers.get("Accept", "")

    def _request_data() -> dict[str, Any]:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            return payload if isinstance(payload, dict) else {}
        return request.form.to_dict()

    def _field(data: dict[str, Any], *names: str, default: Any = "") -> Any:
        for name in names:
            value = data.get(name)
            if value is not None:
                return value
        return default

    def _suite_project_root() -> Path:
        return Path(app.config.get("warmup_suites_project_root") or project_root)

    def _available_suite_context(selected_suite_id: str | None = None) -> dict[str, Any]:
        suites, suite_errors = load_available_suites(_suite_project_root())
        selected = selected_suite_id or DEFAULT_WARMUP_SUITE_ID
        if not any(suite.suite_id == selected for suite in suites):
            selected = DEFAULT_WARMUP_SUITE_ID
        selected_suite_spec = next(
            (s for s in suites if s.suite_id == selected),
            DEFAULT_WARMUP_SUITE,
        )
        return {
            "warmup_suites": suites,
            "warmup_suite_errors": suite_errors,
            "selected_suite_id": selected,
            "selected_suite": selected_suite_spec,
        }

    _REGION_CHOICES = [
        "mypurecloud.com",
        "mypurecloud.ie",
        "mypurecloud.de",
        "mypurecloud.com.au",
        "mypurecloud.jp",
        "apne2.pure.cloud",
        "usw2.pure.cloud",
        "use2.pure.cloud",
        "cac1.pure.cloud",
        "sae1.pure.cloud",
    ]

    def _serialize_for_bootstrap(value: Any) -> Any:
        """Convert Pydantic / dataclass / datetime values into JSON-safe types."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
            return value.to_dict()
        if isinstance(value, dict):
            return {str(k): _serialize_for_bootstrap(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_serialize_for_bootstrap(item) for item in value]
        return str(value)

    def _build_bootstrap_payload(
        *,
        config: "AppConfig",
        report: Optional["TestReport"],
        warmup_metadata: Optional["ModelWarmupRunMetadata"],
        progress_history: list,
        live_progress: Optional[dict[str, Any]],
        history: list[dict[str, Any]],
        schedule_status: dict[str, Any],
        suites: list,
        suite_errors: list[str],
        selected_suite_id: str,
        failure_summaries: list[dict[str, Any]],
        run_active: bool,
        stop_requested: bool,
        active_run_id: Optional[str],
        trigger_source: Optional[str],
        viewing_history_run_id: Optional[str],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "config": {
                "gc_region": config.gc_region,
                "gc_deployment_id": config.gc_deployment_id,
                "response_timeout": config.response_timeout,
                "success_threshold": config.success_threshold,
                "default_attempt_count": getattr(config, "default_attempt_count", MODEL_WARMUP_DEFAULT_ATTEMPTS),
                "default_execution_mode": getattr(config, "default_execution_mode", "serial"),
                "default_worker_count": getattr(config, "default_worker_count", 1),
                "default_pacing_seconds": float(getattr(config, "default_pacing_seconds", 1.0)),
                "default_performance_profile": getattr(config, "default_performance_profile", "safe_adaptive"),
                "default_cadence": getattr(config, "default_cadence", "daily"),
                "default_minute": getattr(config, "default_minute", 0),
                "default_time_hhmm": getattr(config, "default_time_hhmm", "02:00"),
                "default_weekday": getattr(config, "default_weekday", 0),
                "default_day_of_month": getattr(config, "default_day_of_month", 1),
                "default_timezone": getattr(config, "default_timezone", "UTC"),
                "default_schedule_start_date": getattr(config, "default_schedule_start_date", "") or "",
                "default_schedule_end_date": getattr(config, "default_schedule_end_date", "") or "",
            },
            "regions": list(_REGION_CHOICES),
            "suites": [s.to_dict() for s in suites],
            "suite_errors": list(suite_errors),
            "selected_suite_id": selected_suite_id,
            "pacing_choices": sorted(MODEL_WARMUP_PACING_CHOICES),
            "default_attempts": getattr(config, "default_attempt_count", MODEL_WARMUP_DEFAULT_ATTEMPTS),
            "fixed_message": MODEL_WARMUP_FIXED_MESSAGE,
            "run_active": bool(run_active),
            "stop_requested": bool(stop_requested),
            "active_run_id": active_run_id,
            "trigger_source": trigger_source,
            "warmup": _serialize_for_bootstrap(warmup_metadata),
            "report": _serialize_for_bootstrap(report),
            "live_progress": live_progress or {},
            "progress_events": _serialize_for_bootstrap(progress_history),
            "history": _serialize_for_bootstrap(history),
            "schedule_status": _serialize_for_bootstrap(schedule_status),
            "failure_summaries": list(failure_summaries),
            "viewing_history_run_id": viewing_history_run_id,
            "errors": list(errors),
        }

    def _render_mission_control(
        *,
        status_code: int = 200,
        errors: list[str] | None = None,
        selected_suite_id: str | None = None,
        report: Optional["TestReport"] = None,
        run_active: Optional[bool] = None,
        stop_requested: Optional[bool] = None,
        progress_history_override: Optional[list] = None,
        viewing_history_run_id: Optional[str] = None,
        active_nav: str = "cockpit",
        capture_mode: bool = False,
    ):
        suite_ctx = _available_suite_context(selected_suite_id)
        current_config: AppConfig = app.config["app_config"]
        schedule_status = _schedule_store().load()
        app.config["model_warmup_schedule_status"] = schedule_status

        is_run_active = bool(app.config.get("run_active", False)) if run_active is None else bool(run_active)
        is_stop_requested = bool(app.config.get("stop_requested", False)) if stop_requested is None else bool(stop_requested)

        progress_emitter = app.config.get("progress_emitter")
        if progress_history_override is not None:
            progress_history = progress_history_override
        elif isinstance(progress_emitter, ProgressEmitter):
            progress_history = [event.model_dump(mode="json") for event in progress_emitter.get_history(limit=200)]
        else:
            progress_history = []

        history_rows = _build_model_warmup_history(limit=50)
        warmup_metadata = (
            report.model_warmup_run if (report and isinstance(report.model_warmup_run, ModelWarmupRunMetadata)) else None
        )
        if warmup_metadata is None:
            active_meta = app.config.get("active_model_warmup_metadata")
            if isinstance(active_meta, ModelWarmupRunMetadata):
                warmup_metadata = active_meta

        live_progress: Optional[dict[str, Any]] = None
        if isinstance(progress_emitter, ProgressEmitter):
            history_events = progress_emitter.get_history(limit=500)
            live_progress = _build_live_progress_snapshot(history_events, warmup_metadata)

        failure_summaries = _failure_summaries(report)

        bootstrap = _build_bootstrap_payload(
            config=current_config,
            report=report,
            warmup_metadata=warmup_metadata,
            progress_history=progress_history,
            live_progress=live_progress,
            history=history_rows,
            schedule_status=schedule_status,
            suites=suite_ctx["warmup_suites"],
            suite_errors=suite_ctx["warmup_suite_errors"],
            selected_suite_id=suite_ctx["selected_suite_id"],
            failure_summaries=failure_summaries,
            run_active=is_run_active,
            stop_requested=is_stop_requested,
            active_run_id=app.config.get("active_run_id"),
            trigger_source=app.config.get("active_trigger_source") or "manual",
            viewing_history_run_id=viewing_history_run_id,
            errors=errors or [],
        )

        return render_template(
            "mission_control.html",
            config=current_config,
            errors=errors or [],
            warmup_suites=suite_ctx["warmup_suites"],
            warmup_suite_errors=suite_ctx["warmup_suite_errors"],
            selected_suite_id=suite_ctx["selected_suite_id"],
            selected_suite=suite_ctx["selected_suite"],
            report=report,
            warmup=warmup_metadata,
            progress_history=progress_history,
            failure_summaries=failure_summaries,
            run_active=is_run_active,
            stop_requested=is_stop_requested,
            model_warmup_history=history_rows,
            model_warmup_schedule_status=schedule_status,
            viewing_history_run_id=viewing_history_run_id,
            active_nav=active_nav,
            capture_mode=capture_mode,
            default_attempts=current_config.default_attempt_count,
            pacing_choices=sorted(MODEL_WARMUP_PACING_CHOICES),
            fixed_message=MODEL_WARMUP_FIXED_MESSAGE,
            bootstrap_json=(
                json.dumps(bootstrap, default=str)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            ),
        ), status_code

    def _render_home(status_code: int = 200, *, errors: list[str] | None = None, selected_suite_id: str | None = None):
        return _render_mission_control(
            status_code=status_code,
            errors=errors,
            selected_suite_id=selected_suite_id,
            report=None,
            active_nav="cockpit",
        )

    def _history_store() -> RunHistoryStore:
        return app.config["history_store"]

    def _schedule_store() -> ModelWarmupScheduleStore:
        return app.config["model_warmup_schedule_store"]

    def _load_report_by_run_id(run_id: str) -> Optional[TestReport]:
        entry = _history_store().get_entry_by_run_id(run_id)
        if not isinstance(entry, dict):
            return None
        return _history_store().load_report_from_entry(entry)

    def _load_latest_warmup_report() -> tuple[Optional[TestReport], Optional[str]]:
        for entry in _history_store().list_entries(limit=100):
            if not isinstance(entry.get("model_warmup_run"), dict):
                continue
            report = _history_store().load_report_from_entry(entry)
            if report is not None:
                return report, str(entry.get("run_id") or "") or None
        return None, None

    def _build_model_warmup_history(limit: int = 25) -> list[dict]:
        warmup_entries: list[dict] = []
        for entry in _history_store().list_entries(limit=100):
            warmup_summary = entry.get("model_warmup_run")
            if not isinstance(warmup_summary, dict):
                continue
            warmup_entries.append(
                {
                    "run_id": entry.get("run_id"),
                    "suite_name": entry.get("suite_name"),
                    "scenario_name": warmup_summary.get("scenario_name"),
                    "warmup_messages": warmup_summary.get("warmup_messages"),
                    "timestamp": entry.get("timestamp"),
                    "overall_attempts": entry.get("overall_attempts"),
                    "overall_success_rate": entry.get("overall_success_rate"),
                    "duration_seconds": entry.get("duration_seconds"),
                    "storage_type": entry.get("storage_type", "full_json"),
                    "trigger_source": warmup_summary.get("trigger_source", "manual"),
                    "schedule_label": warmup_summary.get("schedule_label"),
                    "scheduled_fire_at_utc": warmup_summary.get("scheduled_fire_at_utc"),
                    "planned_attempts": warmup_summary.get("planned_attempts"),
                    "completed_attempts": warmup_summary.get("completed_attempts"),
                    "attempts_per_second": warmup_summary.get("attempts_per_second"),
                }
            )
            if len(warmup_entries) >= limit:
                break
        return warmup_entries

    def _is_current_run(control: ActiveRunControl) -> bool:
        return app.config.get("active_run_id") == control.run_id

    def _with_stop_metadata(
        report: TestReport,
        *,
        control: ActiveRunControl,
        force_finalized: bool,
    ) -> TestReport:
        enriched = report.model_copy(deep=True)
        finalized_at = control.stop_finalized_at or datetime.now(timezone.utc)
        requested_at = control.stop_requested_at or finalized_at
        enriched.stopped_by_user = True
        enriched.stop_mode = "immediate"
        enriched.force_finalized = bool(force_finalized)
        enriched.stop_requested_at = requested_at
        enriched.stop_finalized_at = finalized_at
        return enriched

    def _record_model_warmup_schedule_completion(
        report: TestReport,
        entry: Optional[dict],
    ) -> None:
        warmup = report.model_warmup_run
        if not isinstance(warmup, ModelWarmupRunMetadata):
            return
        if warmup.trigger_source != "scheduled":
            return
        status = {
            "status": "stopped" if report.stopped_by_user else "completed",
            "trigger_source": "scheduled",
            "schedule_id": warmup.schedule_id,
            "schedule_cadence": warmup.schedule_cadence,
            "schedule_label": warmup.schedule_label,
            "scheduled_fire_at_utc": (
                warmup.scheduled_fire_at_utc.isoformat()
                if warmup.scheduled_fire_at_utc
                else None
            ),
            "run_id": entry.get("run_id") if isinstance(entry, dict) else None,
            "suite_name": warmup.suite_name,
            "scenario_name": warmup.scenario_name,
            "overall_attempts": report.overall_attempts,
            "overall_success_rate": report.overall_success_rate,
            "duration_seconds": report.duration_seconds,
        }
        app.config["model_warmup_schedule_status"] = _schedule_store().record_status(status)

    def _save_report_history(report: TestReport) -> Optional[dict]:
        try:
            entry = _history_store().save_report(report)
            app.config["latest_run_history_entry"] = entry
            return entry
        except Exception:
            app.config["latest_run_history_entry"] = None
            return None

    def _complete_run_if_current(control: ActiveRunControl, report: TestReport) -> bool:
        report_to_store: Optional[TestReport] = None
        with app.config["run_state_lock"]:
            if not _is_current_run(control) or control.finalized:
                return False
            if control.stop_requested_at is not None or report.stopped_by_user:
                if control.stop_finalized_at is None:
                    control.stop_finalized_at = datetime.now(timezone.utc)
                report_to_store = _with_stop_metadata(
                    report,
                    control=control,
                    force_finalized=control.force_finalized,
                )
            else:
                report_to_store = report
            app.config["latest_report"] = report_to_store
            control.finalized = True
            app.config["run_active"] = False
            app.config["stop_requested"] = False
            app.config["active_run_id"] = None
            app.config["active_run_control"] = None
            app.config["active_run_type"] = None
            app.config["active_trigger_source"] = None
            app.config["stop_event"] = threading.Event()
            app.config["active_model_warmup_metadata"] = None
            app.config["scheduled_run_started_at_utc"] = None
        if report_to_store is not None:
            entry = _save_report_history(report_to_store)
            _record_model_warmup_schedule_completion(report_to_store, entry)
        return True

    def _build_partial_report_from_progress(include_empty: bool = False) -> Optional[TestReport]:
        progress_emitter = app.config.get("progress_emitter")
        if not isinstance(progress_emitter, ProgressEmitter):
            return None
        history = progress_emitter.get_history()
        if not history and not include_empty:
            return None
        attempts = [
            event.attempt_result
            for event in history
            if event.event_type == ProgressEventType.ATTEMPT_COMPLETED
            and event.attempt_result is not None
        ]
        if not attempts and not include_empty:
            return None
        threshold = float(app.config["app_config"].success_threshold)
        successes = sum(1 for attempt in attempts if attempt.success)
        timeouts = sum(1 for attempt in attempts if attempt.timed_out)
        skipped = sum(1 for attempt in attempts if attempt.skipped)
        failures = max(0, len(attempts) - successes - timeouts - skipped)
        success_rate = successes / len(attempts) if attempts else 0.0
        warmup_metadata = app.config.get("active_model_warmup_metadata")
        suite_name = (
            warmup_metadata.suite_name
            if isinstance(warmup_metadata, ModelWarmupRunMetadata)
            else MODEL_WARMUP_SUITE_NAME
        )
        scenario_name = (
            warmup_metadata.scenario_name
            if isinstance(warmup_metadata, ModelWarmupRunMetadata)
            else MODEL_WARMUP_SCENARIO_NAME
        )
        started_at = next(
            (
                event.emitted_at
                for event in history
                if event.event_type == ProgressEventType.SUITE_STARTED
            ),
            datetime.now(timezone.utc),
        )
        duration_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - started_at).total_seconds(),
        )
        scenario = {
            "scenario_name": scenario_name,
            "attempts": len(attempts),
            "successes": successes,
            "failures": failures,
            "timeouts": timeouts,
            "skipped": skipped,
            "success_rate": success_rate,
            "is_regression": success_rate < threshold if attempts else False,
            "attempt_results": attempts,
        }
        report = TestReport(
            suite_name=suite_name,
            timestamp=datetime.now(timezone.utc),
            duration_seconds=duration_seconds,
            scenario_results=[scenario] if attempts else [],
            overall_attempts=len(attempts),
            overall_successes=successes,
            overall_failures=failures,
            overall_timeouts=timeouts,
            overall_skipped=skipped,
            overall_success_rate=success_rate,
            has_regressions=bool(attempts and success_rate < threshold),
            regression_threshold=threshold,
        )
        if isinstance(warmup_metadata, ModelWarmupRunMetadata):
            report.model_warmup_run = warmup_metadata.model_copy(
                update={"completed_attempts": len(attempts)}
            )
        return report

    def _build_live_progress_snapshot(history: list, warmup_metadata: object) -> dict[str, Any]:
        planned_attempts = (
            warmup_metadata.planned_attempts
            if isinstance(warmup_metadata, ModelWarmupRunMetadata)
            else 0
        )
        completed_attempts = 0
        started_at = None
        latest_message = None
        latest_event_type = None

        for event in history:
            if event.planned_attempts is not None:
                planned_attempts = max(planned_attempts, int(event.planned_attempts))
            if event.completed_attempts is not None:
                completed_attempts = max(completed_attempts, int(event.completed_attempts))
            if started_at is None and event.event_type == ProgressEventType.SUITE_STARTED:
                started_at = event.emitted_at
            if event.message:
                latest_message = event.message
                latest_event_type = event.event_type.value

        if started_at is None and history:
            started_at = history[0].emitted_at

        now = datetime.now(timezone.utc)
        elapsed_seconds = (
            max(0.0, (now - started_at).total_seconds())
            if started_at is not None
            else 0.0
        )
        attempts_per_second = (
            completed_attempts / elapsed_seconds
            if completed_attempts > 0 and elapsed_seconds > 0
            else 0.0
        )
        remaining_attempts = max(0, planned_attempts - completed_attempts)
        estimated_remaining_seconds = None
        if planned_attempts and remaining_attempts == 0:
            estimated_remaining_seconds = 0.0
        elif attempts_per_second > 0:
            estimated_remaining_seconds = remaining_attempts / attempts_per_second

        return {
            "planned_attempts": planned_attempts,
            "completed_attempts": completed_attempts,
            "remaining_attempts": remaining_attempts,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "attempts_per_second": round(attempts_per_second, 4),
            "estimated_remaining_seconds": (
                round(estimated_remaining_seconds, 3)
                if estimated_remaining_seconds is not None
                else None
            ),
            "percent_complete": (
                round((completed_attempts / planned_attempts) * 100, 2)
                if planned_attempts
                else 0.0
            ),
            "latest_message": latest_message,
            "latest_event_type": latest_event_type,
        }

    def _failure_summaries(report: Optional[TestReport], limit: int = 5) -> list[dict[str, Any]]:
        if report is None:
            return []
        counter: Counter[str] = Counter()
        for scenario in report.scenario_results:
            for attempt in scenario.attempt_results:
                if attempt.success or attempt.skipped:
                    continue
                message = attempt.error or attempt.explanation or "Unknown warm-up failure."
                counter[str(message).strip() or "Unknown warm-up failure."] += 1
        return [
            {"message": message, "count": count}
            for message, count in counter.most_common(limit)
        ]

    def _failure_summary_text(report: Optional[TestReport]) -> str:
        return "; ".join(
            f"{item['count']}x {item['message']}"
            for item in _failure_summaries(report, limit=3)
        )

    def _capture_results_dashboard_png(
        capture_url: str,
        target_selector: str = "#results-performance-card",
    ) -> bytes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "PNG export requires the Python playwright package and Chromium browser."
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1600})
                page.goto(capture_url, wait_until="networkidle", timeout=30000)
                page.emulate_media(media="screen")
                target = page.locator(target_selector).first
                target.wait_for(state="visible", timeout=10000)
                return target.screenshot(type="png")
            finally:
                browser.close()

    def _capture_results_url(history_run_id: Optional[str]) -> str:
        params = {"screenshot": "1"}
        if history_run_id:
            params["history_run_id"] = history_run_id
        return f"{request.host_url.rstrip('/')}{url_for('results')}?{urlencode(params)}"

    def _force_finalize_run(control: ActiveRunControl) -> TestReport:
        now = datetime.now(timezone.utc)
        with app.config["run_state_lock"]:
            control.stop_requested_at = control.stop_requested_at or now
            control.stop_finalized_at = now
            control.force_finalized = True
        partial_report = _build_partial_report_from_progress(include_empty=True)
        if partial_report is None:
            partial_report = TestReport(
                suite_name=MODEL_WARMUP_SUITE_NAME,
                timestamp=now,
                duration_seconds=0.0,
                scenario_results=[],
                overall_attempts=0,
                overall_successes=0,
                overall_failures=0,
                overall_timeouts=0,
                overall_skipped=0,
                overall_success_rate=0.0,
                has_regressions=False,
                regression_threshold=float(app.config["app_config"].success_threshold),
            )
        finalized_report = _with_stop_metadata(
            partial_report,
            control=control,
            force_finalized=True,
        )
        with app.config["run_state_lock"]:
            app.config["latest_report"] = finalized_report
            control.finalized = True
            app.config["run_active"] = False
            app.config["stop_requested"] = False
            if _is_current_run(control):
                app.config["active_run_id"] = None
                app.config["active_run_control"] = None
                app.config["active_run_type"] = None
                app.config["active_trigger_source"] = None
                app.config["stop_event"] = threading.Event()
            app.config["active_model_warmup_metadata"] = None
            app.config["scheduled_run_started_at_utc"] = None
        entry = _save_report_history(finalized_report)
        _record_model_warmup_schedule_completion(finalized_report, entry)
        return finalized_report

    def _parse_model_warmup_request(data: dict[str, Any]) -> tuple[Optional[ModelWarmUpRunRequest], list[str]]:
        current_config: AppConfig = app.config["app_config"]
        deployment_id = str(_field(data, "model_warmup_deployment_id", "deployment_id", default="")).strip()
        region = str(_field(data, "model_warmup_region", "region", default="")).strip()
        recorded_model = str(_field(data, "model_warmup_llm_model", "recorded_model", default="")).strip()
        attempt_count_raw = _field(
            data,
            "model_warmup_attempt_count",
            "attempt_count",
            default=str(current_config.default_attempt_count),
        )
        execution_mode_raw = _field(
            data,
            "model_warmup_execution_mode",
            "execution_mode",
            default=current_config.default_execution_mode,
        )
        worker_count_raw = _field(
            data,
            "model_warmup_parallel_workers",
            "worker_count",
            default=str(current_config.default_worker_count),
        )
        pacing_raw = _field(
            data,
            "model_warmup_pacing_seconds",
            "pacing_seconds",
            default=str(current_config.default_pacing_seconds),
        )
        performance_profile_raw = _field(
            data,
            "model_warmup_performance_profile",
            "performance_profile",
            default=current_config.default_performance_profile,
        )
        suite_id_raw = _field(
            data,
            "model_warmup_suite_id",
            "suite_id",
            default=DEFAULT_WARMUP_SUITE_ID,
        )

        errors: list[str] = []
        try:
            suite_spec = resolve_suite(_suite_project_root(), str(suite_id_raw or DEFAULT_WARMUP_SUITE_ID))
        except ValueError as exc:
            errors.append(str(exc))
            suite_spec = DEFAULT_WARMUP_SUITE
        if not deployment_id:
            errors.append("Deployment ID is required for AVA Spec Warm Up.")
        if not region:
            errors.append("Region is required for AVA Spec Warm Up.")
        try:
            attempt_count = normalize_model_warmup_attempt_count(attempt_count_raw)
        except ValueError as exc:
            errors.append(str(exc))
            attempt_count = current_config.default_attempt_count
        try:
            execution_mode = normalize_model_warmup_execution_mode(str(execution_mode_raw))
        except ValueError as exc:
            errors.append(str(exc))
            execution_mode = "serial"
        try:
            performance_profile = normalize_model_warmup_performance_profile(
                str(performance_profile_raw)
            )
        except ValueError as exc:
            errors.append(str(exc))
            performance_profile = "safe_adaptive"
        try:
            worker_count_unclamped = int(worker_count_raw)
        except (TypeError, ValueError):
            errors.append("AVA Spec Warm Up parallel workers must be a number.")
            worker_count = current_config.default_worker_count
        else:
            if worker_count_unclamped < 1 or worker_count_unclamped > 5:
                errors.append("AVA Spec Warm Up parallel workers must be between 1 and 5.")
            worker_count = normalize_model_warmup_workers(worker_count_unclamped)
        try:
            pacing_seconds = normalize_model_warmup_pacing(pacing_raw)
        except ValueError as exc:
            errors.append(str(exc))
            pacing_seconds = float(current_config.default_pacing_seconds)

        if errors:
            return None, errors
        return (
            ModelWarmUpRunRequest(
                deployment_id=deployment_id,
                region=region,
                recorded_model=recorded_model or None,
                execution_mode=execution_mode,
                worker_count=worker_count,
                pacing_seconds=pacing_seconds,
                performance_profile=performance_profile,
                attempt_count=attempt_count,
                suite_spec=suite_spec,
            ),
            [],
        )

    def _model_warmup_request_to_dict(run_request: ModelWarmUpRunRequest) -> dict[str, Any]:
        return {
            "deployment_id": run_request.deployment_id,
            "region": run_request.region,
            "recorded_model": run_request.recorded_model,
            "execution_mode": run_request.execution_mode,
            "worker_count": run_request.worker_count,
            "pacing_seconds": run_request.pacing_seconds,
            "performance_profile": run_request.performance_profile,
            "attempt_count": run_request.attempt_count,
            "suite_id": run_request.suite_spec.suite_id,
            "suite_spec": run_request.suite_spec.to_dict(),
        }

    def _model_warmup_request_from_dict(payload: dict[str, Any]) -> ModelWarmUpRunRequest:
        current_config: AppConfig = app.config["app_config"]
        suite_payload = payload.get("suite_spec")
        if isinstance(suite_payload, dict):
            suite_spec = suite_from_request_payload(suite_payload)
        else:
            suite_id = str(payload.get("suite_id") or DEFAULT_WARMUP_SUITE_ID).strip()
            suite_spec = resolve_suite(_suite_project_root(), suite_id)
        return ModelWarmUpRunRequest(
            deployment_id=str(payload.get("deployment_id") or "").strip(),
            region=str(payload.get("region") or "").strip(),
            recorded_model=str(payload.get("recorded_model") or "").strip() or None,
            execution_mode=normalize_model_warmup_execution_mode(
                str(payload.get("execution_mode") or current_config.default_execution_mode)
            ),
            worker_count=normalize_model_warmup_workers(
                payload.get("worker_count", current_config.default_worker_count)
            ),
            pacing_seconds=normalize_model_warmup_pacing(
                payload.get("pacing_seconds", current_config.default_pacing_seconds)
            ),
            performance_profile=normalize_model_warmup_performance_profile(
                str(payload.get("performance_profile") or current_config.default_performance_profile)
            ),
            attempt_count=normalize_model_warmup_attempt_count(
                payload.get("attempt_count", current_config.default_attempt_count)
            ),
            suite_spec=suite_spec,
        )

    def _parse_model_warmup_schedule(
        data: dict[str, Any],
        run_request: ModelWarmUpRunRequest,
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        current_config: AppConfig = app.config["app_config"]
        errors: list[str] = []
        try:
            cadence = normalize_model_warmup_schedule_cadence(
                _field(
                    data,
                    "model_warmup_schedule_cadence",
                    "cadence",
                    default=current_config.default_cadence,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))
            cadence = "daily"
        try:
            timezone_name = validate_schedule_timezone_name(
                _field(
                    data,
                    "model_warmup_schedule_timezone",
                    "timezone_name",
                    default=current_config.default_timezone,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))
            timezone_name = "UTC"

        schedule: dict[str, Any] = {
            "cadence": cadence,
            "timezone_name": timezone_name,
            "run_request": _model_warmup_request_to_dict(run_request),
        }
        try:
            start_date, end_date = normalize_schedule_date_range(
                start_date_value=_field(
                    data,
                    "model_warmup_schedule_start_date",
                    "start_date",
                    default=current_config.default_schedule_start_date or "",
                ),
                end_date_value=_field(
                    data,
                    "model_warmup_schedule_end_date",
                    "end_date",
                    default=current_config.default_schedule_end_date or "",
                ),
                timezone_name=timezone_name,
            )
            schedule["start_date"] = start_date
            schedule["end_date"] = end_date
        except ValueError as exc:
            errors.append(str(exc))

        if cadence_interval_hours(cadence) is not None:
            try:
                schedule["minute"] = normalize_schedule_minute(
                    _field(
                        data,
                        "model_warmup_schedule_minute",
                        "minute",
                        default=str(current_config.default_minute),
                    )
                )
            except ValueError as exc:
                errors.append(str(exc))
                schedule["minute"] = current_config.default_minute
        else:
            try:
                hour, minute = parse_schedule_hhmm(
                    _field(
                        data,
                        "model_warmup_schedule_time",
                        "time_hhmm",
                        default=current_config.default_time_hhmm,
                    )
                )
                schedule["time_hhmm"] = f"{hour:02d}:{minute:02d}"
            except ValueError as exc:
                errors.append(str(exc))
                schedule["time_hhmm"] = current_config.default_time_hhmm
            if cadence == "weekly":
                try:
                    schedule["weekday"] = normalize_schedule_weekday(
                        _field(
                            data,
                            "model_warmup_schedule_weekday",
                            "weekday",
                            default=str(current_config.default_weekday),
                        )
                    )
                except ValueError as exc:
                    errors.append(str(exc))
                    schedule["weekday"] = current_config.default_weekday
            if cadence == "monthly":
                try:
                    schedule["day_of_month"] = normalize_schedule_month_day(
                        _field(
                            data,
                            "model_warmup_schedule_day_of_month",
                            "day_of_month",
                            default=str(current_config.default_day_of_month),
                        )
                    )
                except ValueError as exc:
                    errors.append(str(exc))
                    schedule["day_of_month"] = current_config.default_day_of_month

        if errors:
            return None, errors
        schedule["schedule_label"] = model_warmup_schedule_label(schedule)
        next_run = compute_next_model_warmup_run_utc(schedule)
        schedule["next_run_utc"] = next_run.isoformat() if next_run else None
        return schedule, []

    def start_background_model_warmup_run(
        merged_config: AppConfig,
        run_request: ModelWarmUpRunRequest,
        *,
        trigger_source: str = "manual",
        schedule_id: Optional[str] = None,
        scheduled_fire_at_utc: Optional[datetime] = None,
        schedule_cadence: Optional[str] = None,
        schedule_label: Optional[str] = None,
    ) -> str:
        """Start an AVA Spec Warm Up run in a background thread."""

        run_request = replace(
            run_request,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            scheduled_fire_at_utc=scheduled_fire_at_utc,
            schedule_cadence=schedule_cadence,
            schedule_label=schedule_label,
        )
        progress_emitter = ProgressEmitter()
        run_control = ActiveRunControl(run_id=secrets.token_urlsafe(8))
        with app.config["run_state_lock"]:
            app.config["app_config"] = merged_config
            app.config["progress_emitter"] = progress_emitter
            app.config["latest_report"] = None
            app.config["latest_run_history_entry"] = None
            app.config["run_active"] = True
            app.config["stop_requested"] = False
            app.config["stop_event"] = run_control.stop_event
            app.config["active_run_control"] = run_control
            app.config["active_run_id"] = run_control.run_id
            app.config["active_run_type"] = "model_warm_up"
            app.config["active_trigger_source"] = trigger_source
            app.config["active_model_warmup_metadata"] = build_model_warmup_metadata(
                run_request
            )
            app.config["history_store"] = RunHistoryStore(
                history_dir=merged_config.history_dir,
                max_runs=merged_config.history_max_runs,
                full_json_runs=merged_config.history_full_json_runs,
                gzip_runs=merged_config.history_gzip_runs,
            )
            app.config["scheduled_run_started_at_utc"] = (
                datetime.now(timezone.utc).isoformat()
                if trigger_source == "scheduled"
                else None
            )

        def run_tests() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                runner = ModelWarmUpRunner(
                    config=merged_config,
                    progress_emitter=progress_emitter,
                    stop_event=run_control.stop_event,
                )
                report = loop.run_until_complete(runner.run(run_request))
                _complete_run_if_current(run_control, report)
            finally:
                with app.config["run_state_lock"]:
                    if _is_current_run(run_control) and not run_control.finalized:
                        app.config["run_active"] = False
                        app.config["stop_requested"] = False
                        app.config["active_run_id"] = None
                        app.config["active_run_control"] = None
                        app.config["active_run_type"] = None
                        app.config["active_trigger_source"] = None
                        app.config["stop_event"] = threading.Event()
                    if not app.config.get("run_active", False):
                        app.config["active_model_warmup_metadata"] = None
                        app.config["scheduled_run_started_at_utc"] = None
                loop.close()

        thread = threading.Thread(target=run_tests, daemon=True)
        run_control.thread = thread
        thread.start()
        return run_control.run_id

    def _record_model_warmup_schedule_status(status: dict[str, Any]) -> None:
        app.config["model_warmup_schedule_status"] = _schedule_store().record_status(status)

    def run_scheduled_model_warmup(
        settings: dict[str, Any],
        scheduled_fire_at_utc: datetime,
    ) -> None:
        schedule_id = str(settings.get("schedule_id") or "")
        schedule_label = str(settings.get("schedule_label") or model_warmup_schedule_label(settings))
        if app.config.get("run_active", False):
            _record_model_warmup_schedule_status(
                {
                    "status": "skipped",
                    "reason": "another_run_active",
                    "trigger_source": "scheduled",
                    "schedule_id": schedule_id,
                    "schedule_cadence": settings.get("cadence"),
                    "schedule_label": schedule_label,
                    "scheduled_fire_at_utc": scheduled_fire_at_utc.astimezone(timezone.utc).isoformat(),
                }
            )
            return
        try:
            run_request = _model_warmup_request_from_dict(settings.get("run_request") or {})
            base_cfg = app.config["app_config"]
            merged_cfg = merge_config(
                base_cfg,
                {
                    "gc_deployment_id": run_request.deployment_id,
                    "gc_region": run_request.region,
                },
            )
            app.config["last_run_config"] = merged_cfg.model_copy(deep=True)
            _record_model_warmup_schedule_status(
                {
                    "status": "started",
                    "trigger_source": "scheduled",
                    "schedule_id": schedule_id,
                    "schedule_cadence": settings.get("cadence"),
                    "schedule_label": schedule_label,
                    "scheduled_fire_at_utc": scheduled_fire_at_utc.astimezone(timezone.utc).isoformat(),
                }
            )
            start_background_model_warmup_run(
                merged_cfg,
                run_request,
                trigger_source="scheduled",
                schedule_id=schedule_id,
                scheduled_fire_at_utc=scheduled_fire_at_utc.astimezone(timezone.utc),
                schedule_cadence=str(settings.get("cadence") or ""),
                schedule_label=schedule_label,
            )
        except Exception as exc:
            _record_model_warmup_schedule_status(
                {
                    "status": "failed",
                    "reason": str(exc),
                    "trigger_source": "scheduled",
                    "schedule_id": schedule_id,
                    "schedule_cadence": settings.get("cadence"),
                    "schedule_label": schedule_label,
                    "scheduled_fire_at_utc": scheduled_fire_at_utc.astimezone(timezone.utc).isoformat(),
                }
            )

    def ensure_model_warmup_scheduler_state() -> None:
        status = _schedule_store().load()
        app.config["model_warmup_schedule_status"] = status
        scheduler = app.config.get("model_warmup_scheduler")
        enabled = bool(status.get("enabled"))
        if enabled and not isinstance(scheduler, ModelWarmupScheduler):
            scheduler = ModelWarmupScheduler(
                settings_getter=lambda: _schedule_store().load(),
                run_job=run_scheduled_model_warmup,
                next_run_updater=lambda next_run: _schedule_store().update_next_run(next_run),
                completion_handler=lambda: _schedule_store().complete_date_range(),
            )
            scheduler.start()
            app.config["model_warmup_scheduler"] = scheduler
            return
        if not enabled and isinstance(scheduler, ModelWarmupScheduler):
            scheduler.stop()
            app.config["model_warmup_scheduler"] = None

    def _bootstrap_schedule_from_env() -> None:
        """Apply an env-driven schedule on startup when auto-schedule is enabled.

        Designed for DigitalOcean App Platform where the local filesystem is
        ephemeral: a redeploy can wipe `model_warmup_schedule.json`, so we
        re-create it from env every boot when `AVA_WARMUP_AUTO_SCHEDULE_ENABLED`
        is true. UI-saved schedules (``source == "user"``) take precedence and
        are NOT overwritten — env vars are the seed; UI edits win once made.
        """

        current_config: AppConfig = app.config["app_config"]
        if not current_config.auto_schedule_enabled:
            return
        existing = _schedule_store().load() or {}
        if str(existing.get("source") or "").lower() == "user":
            return
        deployment_id = (current_config.gc_deployment_id or "").strip()
        region = (current_config.gc_region or "").strip()
        if not deployment_id or not region:
            return
        if not current_config.default_schedule_end_date:
            return
        try:
            suite_spec = resolve_suite(_suite_project_root(), DEFAULT_WARMUP_SUITE_ID)
            run_request = ModelWarmUpRunRequest(
                deployment_id=deployment_id,
                region=region,
                execution_mode=normalize_model_warmup_execution_mode(
                    current_config.default_execution_mode
                ),
                worker_count=normalize_model_warmup_workers(
                    current_config.default_worker_count
                ),
                pacing_seconds=normalize_model_warmup_pacing(
                    current_config.default_pacing_seconds
                ),
                performance_profile=normalize_model_warmup_performance_profile(
                    current_config.default_performance_profile
                ),
                attempt_count=normalize_model_warmup_attempt_count(
                    current_config.default_attempt_count
                ),
                suite_spec=suite_spec,
            )
            cadence = normalize_model_warmup_schedule_cadence(
                current_config.default_cadence
            )
            timezone_name = validate_schedule_timezone_name(
                current_config.default_timezone
            )
            start_date, end_date = normalize_schedule_date_range(
                start_date_value=current_config.default_schedule_start_date or "",
                end_date_value=current_config.default_schedule_end_date,
                timezone_name=timezone_name,
            )
            schedule_payload: dict[str, Any] = {
                "cadence": cadence,
                "timezone_name": timezone_name,
                "start_date": start_date,
                "end_date": end_date,
                "run_request": _model_warmup_request_to_dict(run_request),
            }
            if cadence_interval_hours(cadence) is not None:
                schedule_payload["minute"] = normalize_schedule_minute(
                    current_config.default_minute
                )
            else:
                hour, minute = parse_schedule_hhmm(current_config.default_time_hhmm)
                schedule_payload["time_hhmm"] = f"{hour:02d}:{minute:02d}"
                if cadence == "weekly":
                    schedule_payload["weekday"] = normalize_schedule_weekday(
                        current_config.default_weekday
                    )
                if cadence == "monthly":
                    schedule_payload["day_of_month"] = normalize_schedule_month_day(
                        current_config.default_day_of_month
                    )
        except ValueError:
            return
        schedule_payload["schedule_label"] = model_warmup_schedule_label(schedule_payload)
        schedule_payload["source"] = "env"
        app.config["model_warmup_schedule_status"] = _schedule_store().save_schedule(
            schedule_payload
        )

    _bootstrap_schedule_from_env()
    ensure_model_warmup_scheduler_state()

    _AUTH_EXEMPT_ENDPOINTS: set[str] = {"login", "logout", "static", "healthz"}

    def _admin_credentials() -> tuple[Optional[str], Optional[str]]:
        cfg: AppConfig = app.config["app_config"]
        return cfg.admin_user, cfg.admin_password

    def _auth_configured() -> bool:
        user, password = _admin_credentials()
        return bool(user and password)

    def _is_authenticated() -> bool:
        if not _auth_configured():
            return False
        user, _ = _admin_credentials()
        return session.get("authenticated") is True and session.get("user") == user

    def _safe_next_path(candidate: Optional[str]) -> Optional[str]:
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc:
            return None
        if not candidate.startswith("/"):
            return None
        if candidate.startswith("//"):
            return None
        return candidate

    @app.before_request
    def _require_login():
        if not _auth_configured():
            return (
                "Authentication is not configured. "
                "Set ADMIN_USER and ADMIN_PASSWORD environment variables.",
                503,
            )
        endpoint = request.endpoint or ""
        if endpoint in _AUTH_EXEMPT_ENDPOINTS:
            return None
        if _is_authenticated():
            return None
        if _wants_json():
            return jsonify({"ok": False, "error": "Authentication required."}), 401
        next_path = _safe_next_path(request.full_path if request.query_string else request.path)
        return redirect(url_for("login", next=next_path) if next_path else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not _auth_configured():
            return (
                "Authentication is not configured. "
                "Set ADMIN_USER and ADMIN_PASSWORD environment variables.",
                503,
            )
        next_path = _safe_next_path(request.args.get("next") or request.form.get("next"))
        if _is_authenticated():
            return redirect(next_path or url_for("home"))

        error: Optional[str] = None
        if request.method == "POST":
            submitted_user = (request.form.get("username") or "").strip()
            submitted_password = request.form.get("password") or ""
            admin_user, admin_password = _admin_credentials()
            user_ok = hmac.compare_digest(submitted_user, admin_user or "")
            password_ok = hmac.compare_digest(submitted_password, admin_password or "")
            if user_ok and password_ok:
                session.clear()
                session["authenticated"] = True
                session["user"] = admin_user
                session.permanent = False
                return redirect(next_path or url_for("home"))
            error = "Invalid username or password."

        return render_template("login.html", error=error, next_path=next_path or ""), (
            401 if error else 200
        )

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.route("/")
    def home():
        schedule_status = _schedule_store().load()
        app.config["model_warmup_schedule_status"] = schedule_status
        return _render_home()

    @app.route("/run/model_warm_up", methods=["POST"])
    def run_model_warm_up():
        if app.config.get("run_active", False):
            if _wants_json():
                return jsonify({"ok": False, "error": "A warm-up run is already active."}), 409
            return redirect(url_for("results"))

        data = _request_data()
        run_request, errors = _parse_model_warmup_request(data)
        if errors or run_request is None:
            if _wants_json():
                return jsonify({"ok": False, "errors": errors}), 400
            return _render_home(
                400,
                errors=errors,
                selected_suite_id=str(
                    _field(data, "model_warmup_suite_id", "suite_id", default=DEFAULT_WARMUP_SUITE_ID)
                ),
            )

        base_config = app.config["app_config"]
        merged_config = merge_config(
            base_config,
            {
                "gc_deployment_id": run_request.deployment_id,
                "gc_region": run_request.region,
            },
        )
        app.config["last_run_config"] = merged_config.model_copy(deep=True)
        run_id = start_background_model_warmup_run(merged_config, run_request)
        if _wants_json():
            return jsonify({"ok": True, "run_id": run_id, "results_url": url_for("results")}), 202
        return redirect(url_for("results"))

    @app.route("/run/stop", methods=["POST"])
    def stop_run():
        control = app.config.get("active_run_control")
        if isinstance(control, ActiveRunControl):
            now = datetime.now(timezone.utc)
            with app.config["run_state_lock"]:
                control.stop_requested_at = control.stop_requested_at or now
                app.config["stop_requested"] = True
                control.stop_event.set()
            if _wants_json():
                return jsonify({"ok": True, "stop_requested": True})
            flash("AVA Spec Warm Up stop requested.")
            return redirect(url_for("results"))
        if _wants_json():
            return jsonify({"ok": False, "error": "No active warm-up run."}), 409
        return redirect(url_for("results"))

    @app.route("/run/status")
    def run_status():
        run_active = bool(app.config.get("run_active", False))
        warmup_metadata = app.config.get("active_model_warmup_metadata")
        progress_emitter = app.config.get("progress_emitter")
        history_events = (
            progress_emitter.get_history(limit=500)
            if isinstance(progress_emitter, ProgressEmitter)
            else []
        )
        live_progress = _build_live_progress_snapshot(history_events, warmup_metadata)
        warmup_payload = (
            warmup_metadata.model_copy(
                update={
                    "completed_attempts": live_progress["completed_attempts"],
                    "attempts_per_second": live_progress["attempts_per_second"] or None,
                }
            ).model_dump(mode="json")
            if isinstance(warmup_metadata, ModelWarmupRunMetadata)
            else None
        )
        progress_history = [event.model_dump(mode="json") for event in history_events[-100:]]
        return jsonify(
            {
                "run_active": run_active,
                "active_run_id": app.config.get("active_run_id"),
                "run_type": app.config.get("active_run_type") if run_active else None,
                "trigger_source": app.config.get("active_trigger_source") if run_active else "manual",
                "scheduled_run_started_at_utc": app.config.get("scheduled_run_started_at_utc"),
                "stop_requested": bool(app.config.get("stop_requested", False)),
                "model_warmup_run": warmup_payload,
                "live_progress": live_progress,
                "progress": progress_history,
            }
        )

    @app.route("/run/model_warm_up/schedule", methods=["POST"])
    def save_model_warmup_schedule():
        data = _request_data()
        run_request, errors = _parse_model_warmup_request(data)
        if run_request is None:
            run_request = ModelWarmUpRunRequest(deployment_id="invalid", region="invalid")
        schedule_payload, schedule_errors = _parse_model_warmup_schedule(data, run_request)
        errors.extend(schedule_errors)
        if errors or schedule_payload is None:
            if _wants_json():
                return jsonify({"ok": False, "errors": errors}), 400
            return _render_home(
                400,
                errors=errors,
                selected_suite_id=str(
                    _field(data, "model_warmup_suite_id", "suite_id", default=DEFAULT_WARMUP_SUITE_ID)
                ),
            )
        schedule_payload["source"] = "user"
        app.config["model_warmup_schedule_status"] = _schedule_store().save_schedule(
            schedule_payload
        )
        ensure_model_warmup_scheduler_state()
        if _wants_json():
            return jsonify({"ok": True, "schedule": app.config["model_warmup_schedule_status"]})
        flash("AVA Spec Warm Up schedule saved.")
        return redirect(url_for("home"))

    @app.route("/run/model_warm_up/schedule/disable", methods=["POST"])
    @app.route("/run/model_warm_up/schedule/cancel", methods=["POST"])
    def disable_model_warmup_schedule():
        app.config["model_warmup_schedule_status"] = _schedule_store().disable()
        ensure_model_warmup_scheduler_state()
        if _wants_json():
            return jsonify({"ok": True, "schedule": app.config["model_warmup_schedule_status"]})
        flash("AVA Spec Warm Up schedule canceled.")
        return redirect(url_for("results") if request.form.get("model_warmup_schedule_redirect") == "results" else url_for("home"))

    @app.route("/run/model_warm_up/schedule/status")
    def model_warmup_schedule_status():
        status = _schedule_store().load()
        app.config["model_warmup_schedule_status"] = status
        return jsonify(status)

    @app.route("/results")
    def results():
        history_run_id = request.args.get("history_run_id", "").strip() or None
        report = _load_report_by_run_id(history_run_id) if history_run_id else None
        viewing_history_report = report is not None
        if report is None:
            report = app.config.get("latest_report")
        if report is None and app.config.get("run_active", False):
            report = _build_partial_report_from_progress(include_empty=True)
        if report is None:
            report, latest_history_run_id = _load_latest_warmup_report()
            if report is not None:
                history_run_id = latest_history_run_id
                viewing_history_report = True
        body, _status = _render_mission_control(
            report=report,
            viewing_history_run_id=(history_run_id if viewing_history_report else None),
            capture_mode=request.args.get("screenshot") == "1",
            active_nav="cockpit",
        )
        return body

    @app.route("/results/history")
    def results_history():
        limit = request.args.get("limit", type=int)
        if limit is None:
            limit = 100
        limit = max(1, min(limit, 100))
        entries = _history_store().list_entries(limit=limit)
        runs = [
            {
                "run_id": entry.get("run_id"),
                "suite_name": entry.get("suite_name"),
                "timestamp": entry.get("timestamp"),
                "overall_attempts": entry.get("overall_attempts"),
                "overall_successes": entry.get("overall_successes"),
                "overall_failures": entry.get("overall_failures"),
                "overall_timeouts": entry.get("overall_timeouts"),
                "overall_skipped": entry.get("overall_skipped"),
                "overall_success_rate": entry.get("overall_success_rate"),
                "duration_seconds": entry.get("duration_seconds"),
                "has_regressions": entry.get("has_regressions"),
                "storage_type": entry.get("storage_type", "full_json"),
                "run_type": entry.get("run_type", "model_warm_up"),
                "model_warmup_run": entry.get("model_warmup_run"),
            }
            for entry in entries
        ]
        return jsonify({"runs": runs})

    def _report_for_export() -> Optional[TestReport]:
        history_run_id = request.args.get("history_run_id", "").strip() or None
        if history_run_id:
            return _load_report_by_run_id(history_run_id)
        report = app.config.get("latest_report")
        if isinstance(report, TestReport):
            return report
        partial_report = _build_partial_report_from_progress()
        if partial_report is not None:
            return partial_report
        latest_report, _ = _load_latest_warmup_report()
        return latest_report

    @app.route("/results/export")
    def export_results():
        report = _report_for_export()
        if report is None:
            return jsonify({"error": "No AVA Spec Warm Up report is available."}), 404
        export_format = request.args.get("format", "json").strip().lower()
        if export_format == "png":
            history_run_id = request.args.get("history_run_id", "").strip() or None
            try:
                capture_func = app.config.get(
                    "results_png_capture",
                    _capture_results_dashboard_png,
                )
                png_data = capture_func(
                    _capture_results_url(history_run_id),
                    "#results-performance-card",
                )
            except Exception as exc:
                return jsonify({"error": f"Unable to export PNG: {exc}"}), 503
            return send_file(
                io.BytesIO(png_data),
                mimetype="image/png",
                as_attachment=True,
                download_name="ava_spec_warm_up_results.png",
            )
        if export_format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "suite_name",
                    "scenario_name",
                    "fixed_message",
                    "warmup_messages",
                    "timestamp",
                    "planned_attempts",
                    "completed_attempts",
                    "successes",
                    "failures",
                    "timeouts",
                    "skipped",
                    "success_rate",
                    "attempts_per_second",
                    "execution_mode",
                    "worker_count",
                    "effective_worker_count",
                    "pacing_seconds",
                    "effective_pacing_seconds",
                    "trigger_source",
                    "schedule_label",
                    "failure_summary",
                ],
            )
            writer.writeheader()
            warmup = report.model_warmup_run
            writer.writerow(
                {
                    "suite_name": report.suite_name,
                    "scenario_name": warmup.scenario_name if warmup else None,
                    "fixed_message": warmup.fixed_message if warmup else None,
                    "warmup_messages": " | ".join(warmup.warmup_messages) if warmup else None,
                    "timestamp": report.timestamp.isoformat(),
                    "planned_attempts": warmup.planned_attempts if warmup else report.overall_attempts,
                    "completed_attempts": report.overall_attempts,
                    "successes": report.overall_successes,
                    "failures": report.overall_failures,
                    "timeouts": report.overall_timeouts,
                    "skipped": report.overall_skipped,
                    "success_rate": report.overall_success_rate,
                    "attempts_per_second": warmup.attempts_per_second if warmup else None,
                    "execution_mode": warmup.execution_mode if warmup else None,
                    "worker_count": warmup.worker_count if warmup else None,
                    "effective_worker_count": warmup.effective_worker_count if warmup else None,
                    "pacing_seconds": warmup.pacing_seconds if warmup else None,
                    "effective_pacing_seconds": warmup.effective_pacing_seconds if warmup else None,
                    "trigger_source": warmup.trigger_source if warmup else "manual",
                    "schedule_label": warmup.schedule_label if warmup else None,
                    "failure_summary": _failure_summary_text(report),
                }
            )
            data = io.BytesIO(output.getvalue().encode("utf-8"))
            return send_file(
                data,
                mimetype="text/csv",
                as_attachment=True,
                download_name="ava_spec_warm_up_metrics.csv",
            )
        return jsonify(report.model_dump(mode="json"))

    app.start_background_model_warmup_run = start_background_model_warmup_run  # type: ignore[attr-defined]
    app.ensure_model_warmup_scheduler_state = ensure_model_warmup_scheduler_state  # type: ignore[attr-defined]
    app.force_finalize_run = _force_finalize_run  # type: ignore[attr-defined]
    return app


app = create_app()


if __name__ == "__main__":
    _runtime_config = load_app_config()
    app.run(host=_runtime_config.server_host, port=_runtime_config.server_port, debug=False)
