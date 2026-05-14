"""Persistent scheduler for AVA Spec Warm Up automation."""

from __future__ import annotations

import calendar
import json
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

MODEL_WARMUP_SCHEDULE_CADENCES = {"hourly", "daily", "weekly", "monthly"}
MODEL_WARMUP_SCHEDULE_FILE = "model_warmup_schedule.json"
MODEL_WARMUP_DATE_RANGE_STATUS_PENDING = "pending"
MODEL_WARMUP_DATE_RANGE_STATUS_ACTIVE = "active"
MODEL_WARMUP_DATE_RANGE_STATUS_COMPLETED = "completed"
MODEL_WARMUP_DATE_RANGE_STATUS_CANCELED = "canceled"


def cadence_interval_hours(cadence: str) -> Optional[int]:
    """Return the hour interval for hourly or numeric ("every N hours") cadences.

    Returns 1 for ``hourly``, ``N`` for a positive-integer string like ``"3"``,
    and ``None`` for ``daily``/``weekly``/``monthly`` or any non-interval value.
    """

    normalized = str(cadence or "").strip().lower()
    if normalized == "hourly":
        return 1
    try:
        value = int(normalized)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return value


def normalize_model_warmup_schedule_cadence(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in MODEL_WARMUP_SCHEDULE_CADENCES:
        return normalized
    interval = cadence_interval_hours(normalized)
    if interval is not None:
        return str(interval)
    raise ValueError(
        "AVA Spec Warm Up schedule cadence must be hourly, daily, weekly, monthly, "
        "or a positive integer number of hours."
    )


def parse_schedule_hhmm(value: str) -> tuple[int, int]:
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError("AVA Spec Warm Up schedule time must use HH:MM format.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("AVA Spec Warm Up schedule time must use HH:MM format.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("AVA Spec Warm Up schedule time must use HH:MM format.")
    return hour, minute


def normalize_schedule_minute(value: Any) -> int:
    try:
        minute = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hourly AVA Spec Warm Up minute must be between 0 and 59.") from exc
    if minute < 0 or minute > 59:
        raise ValueError("Hourly AVA Spec Warm Up minute must be between 0 and 59.")
    return minute


def normalize_schedule_weekday(value: Any) -> int:
    try:
        weekday = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Weekly AVA Spec Warm Up weekday must be between 0 and 6.") from exc
    if weekday < 0 or weekday > 6:
        raise ValueError("Weekly AVA Spec Warm Up weekday must be between 0 and 6.")
    return weekday


def normalize_schedule_month_day(value: Any) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Monthly AVA Spec Warm Up day must be between 1 and 31.") from exc
    if day < 1 or day > 31:
        raise ValueError("Monthly AVA Spec Warm Up day must be between 1 and 31.")
    return day


def resolve_schedule_timezone(timezone_name: Optional[str]):
    raw = str(timezone_name or "").strip()
    if not raw:
        return timezone.utc
    try:
        return ZoneInfo(raw)
    except Exception:
        return timezone.utc


def validate_schedule_timezone_name(timezone_name: str) -> str:
    normalized = str(timezone_name or "").strip()
    if not normalized:
        return "UTC"
    try:
        ZoneInfo(normalized)
    except Exception as exc:
        raise ValueError(f"Invalid AVA Spec Warm Up schedule timezone: {normalized}") from exc
    return normalized


def parse_schedule_date(value: Any, *, field_name: str = "date") -> date:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"AVA Spec Warm Up schedule {field_name} is required.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"AVA Spec Warm Up schedule {field_name} must use YYYY-MM-DD format."
        ) from exc


def normalize_schedule_date_range(
    *,
    start_date_value: Any,
    end_date_value: Any,
    timezone_name: str,
    now_utc: Optional[datetime] = None,
) -> tuple[str, str]:
    tzinfo = resolve_schedule_timezone(timezone_name)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    default_start = now.astimezone(tzinfo).date()
    raw_start = str(start_date_value or "").strip()
    start_date = parse_schedule_date(raw_start, field_name="start date") if raw_start else default_start
    end_date = parse_schedule_date(end_date_value, field_name="end date")
    if end_date < start_date:
        raise ValueError("AVA Spec Warm Up schedule end date must be on or after start date.")
    return start_date.isoformat(), end_date.isoformat()


def _optional_schedule_date(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def model_warmup_schedule_date_range_status(
    settings: dict[str, Any],
    *,
    now_utc: Optional[datetime] = None,
) -> str:
    enabled = bool(settings.get("enabled"))
    last_status = settings.get("last_status") if isinstance(settings, dict) else None
    last_status_name = (
        str(last_status.get("status") or "").strip().lower()
        if isinstance(last_status, dict)
        else ""
    )
    if not enabled:
        if settings.get("completed_at_utc") or last_status_name == "completed":
            return MODEL_WARMUP_DATE_RANGE_STATUS_COMPLETED
        return MODEL_WARMUP_DATE_RANGE_STATUS_CANCELED

    start_date = _optional_schedule_date(settings.get("start_date"))
    end_date = _optional_schedule_date(settings.get("end_date"))
    if start_date is None or end_date is None:
        return MODEL_WARMUP_DATE_RANGE_STATUS_ACTIVE

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_today = now.astimezone(resolve_schedule_timezone(settings.get("timezone_name"))).date()
    if local_today < start_date:
        return MODEL_WARMUP_DATE_RANGE_STATUS_PENDING
    if local_today > end_date:
        return MODEL_WARMUP_DATE_RANGE_STATUS_COMPLETED
    return MODEL_WARMUP_DATE_RANGE_STATUS_ACTIVE


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _monthly_candidate(
    *,
    year: int,
    month: int,
    requested_day: int,
    hour: int,
    minute: int,
    tzinfo,
) -> datetime:
    day = min(requested_day, _last_day_of_month(year, month))
    return datetime.combine(
        datetime(year, month, day).date(),
        time(hour=hour, minute=minute),
        tzinfo=tzinfo,
    )


def compute_next_model_warmup_run_utc(
    settings: dict[str, Any],
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[datetime]:
    """Compute the next future UTC fire time for an AVA Spec Warm Up schedule."""

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    tzinfo = resolve_schedule_timezone(settings.get("timezone_name"))
    local_now = now.astimezone(tzinfo)
    cadence = normalize_model_warmup_schedule_cadence(str(settings.get("cadence") or "daily"))
    start_date = _optional_schedule_date(settings.get("start_date"))
    end_date = _optional_schedule_date(settings.get("end_date"))
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("AVA Spec Warm Up schedule end date must be on or after start date.")
    if end_date is not None and local_now.date() > end_date:
        return None
    if start_date is not None and local_now.date() < start_date:
        local_now = datetime.combine(start_date, time(0, 0), tzinfo=tzinfo) - timedelta(microseconds=1)

    interval_hours = cadence_interval_hours(cadence)

    def _candidate_after(anchor: datetime) -> datetime:
        if interval_hours is not None:
            minute = normalize_schedule_minute(settings.get("minute", 0))
            if interval_hours == 1:
                hourly_candidate = anchor.replace(minute=minute, second=0, microsecond=0)
                if hourly_candidate <= anchor:
                    hourly_candidate += timedelta(hours=1)
                return hourly_candidate
            grid_date = start_date or anchor.date()
            grid_anchor = datetime.combine(grid_date, time(0, minute), tzinfo=tzinfo)
            if grid_anchor > anchor:
                return grid_anchor
            step_seconds = interval_hours * 3600
            elapsed_seconds = (anchor - grid_anchor).total_seconds()
            steps = int(elapsed_seconds // step_seconds) + 1
            return grid_anchor + timedelta(hours=interval_hours * steps)

        hour, minute = parse_schedule_hhmm(str(settings.get("time_hhmm") or "02:00"))
        if cadence == "daily":
            daily_candidate = datetime.combine(anchor.date(), time(hour=hour, minute=minute), tzinfo=tzinfo)
            if daily_candidate <= anchor:
                daily_candidate += timedelta(days=1)
            return daily_candidate
        if cadence == "weekly":
            weekday = normalize_schedule_weekday(settings.get("weekday", 0))
            days_ahead = (weekday - anchor.weekday()) % 7
            candidate_date = anchor.date() + timedelta(days=days_ahead)
            weekly_candidate = datetime.combine(candidate_date, time(hour=hour, minute=minute), tzinfo=tzinfo)
            if weekly_candidate <= anchor:
                weekly_candidate += timedelta(days=7)
            return weekly_candidate

        requested_day = normalize_schedule_month_day(settings.get("day_of_month", 1))
        monthly_candidate = _monthly_candidate(
            year=anchor.year,
            month=anchor.month,
            requested_day=requested_day,
            hour=hour,
            minute=minute,
            tzinfo=tzinfo,
        )
        if monthly_candidate <= anchor:
            year, month = _add_month(anchor.year, anchor.month)
            monthly_candidate = _monthly_candidate(
                year=year,
                month=month,
                requested_day=requested_day,
                hour=hour,
                minute=minute,
                tzinfo=tzinfo,
            )
        return monthly_candidate

    def _advance_candidate(candidate: datetime) -> datetime:
        if interval_hours is not None:
            return candidate + timedelta(hours=interval_hours)
        if cadence == "daily":
            return candidate + timedelta(days=1)
        if cadence == "weekly":
            return candidate + timedelta(days=7)
        requested_day = normalize_schedule_month_day(settings.get("day_of_month", 1))
        hour, minute = parse_schedule_hhmm(str(settings.get("time_hhmm") or "02:00"))
        year, month = _add_month(candidate.year, candidate.month)
        return _monthly_candidate(
            year=year,
            month=month,
            requested_day=requested_day,
            hour=hour,
            minute=minute,
            tzinfo=tzinfo,
        )

    candidate = _candidate_after(local_now)
    while start_date is not None and candidate.date() < start_date:
        candidate = _advance_candidate(candidate)
    if end_date is not None and candidate.date() > end_date:
        return None
    return candidate.astimezone(timezone.utc)


def model_warmup_schedule_label(settings: dict[str, Any]) -> str:
    cadence = str(settings.get("cadence") or "").strip().lower()
    timezone_name = str(settings.get("timezone_name") or "UTC")
    interval_hours = cadence_interval_hours(cadence)
    if interval_hours is not None:
        minute = int(settings.get("minute", 0))
        if interval_hours == 1:
            return f"Hourly at minute {minute:02d} ({timezone_name})"
        return f"Every {interval_hours} hours at minute {minute:02d} ({timezone_name})"
    if cadence == "daily":
        return f"Daily at {settings.get('time_hhmm', '02:00')} ({timezone_name})"
    if cadence == "weekly":
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        weekday = normalize_schedule_weekday(settings.get("weekday", 0))
        return f"Weekly on {weekdays[weekday]} at {settings.get('time_hhmm', '02:00')} ({timezone_name})"
    if cadence == "monthly":
        return f"Monthly on day {int(settings.get('day_of_month', 1))} at {settings.get('time_hhmm', '02:00')} ({timezone_name})"
    return "Disabled"


class ModelWarmupScheduleStore:
    """Persist the single AVA Spec Warm Up schedule and latest status."""

    def __init__(self, *, history_dir: str):
        self.path = Path(history_dir) / MODEL_WARMUP_SCHEDULE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"enabled": False, "scheduled_warmups": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"enabled": False, "scheduled_warmups": []}
        if not isinstance(payload, dict):
            return {"enabled": False, "scheduled_warmups": []}
        if (
            bool(payload.get("enabled"))
            and model_warmup_schedule_date_range_status(payload)
            == MODEL_WARMUP_DATE_RANGE_STATUS_COMPLETED
        ):
            return self._write(self._mark_date_range_completed(payload))
        return self._with_view(payload)

    def save_schedule(self, settings: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        existing = self.load()
        payload = dict(settings)
        payload["enabled"] = True
        payload["schedule_id"] = str(existing.get("schedule_id") or uuid.uuid4().hex)
        payload["created_at_utc"] = str(existing.get("created_at_utc") or now.isoformat())
        payload["updated_at_utc"] = now.isoformat()
        payload["schedule_label"] = model_warmup_schedule_label(payload)
        next_run = compute_next_model_warmup_run_utc(payload, now_utc=now)
        payload.pop("canceled_at_utc", None)
        payload.pop("completed_at_utc", None)
        if next_run is None:
            payload = self._mark_date_range_completed(payload, now=now)
        else:
            payload["next_run_utc"] = next_run.isoformat()
            payload["last_status"] = {
                "status": "scheduled",
                "reason": "schedule_saved",
                "schedule_id": payload["schedule_id"],
                "schedule_label": payload["schedule_label"],
                "next_run_utc": payload["next_run_utc"],
                "recorded_at_utc": now.isoformat(),
            }
        return self._write(payload)

    def disable(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload = self.load()
        payload["enabled"] = False
        payload["updated_at_utc"] = now.isoformat()
        payload["canceled_at_utc"] = now.isoformat()
        payload["next_run_utc"] = None
        if payload.get("schedule_id"):
            payload["last_status"] = {
                "status": "canceled",
                "reason": "user_canceled",
                "schedule_id": payload.get("schedule_id"),
                "schedule_label": payload.get("schedule_label"),
                "canceled_at_utc": now.isoformat(),
                "recorded_at_utc": now.isoformat(),
            }
        return self._write(payload)

    def complete_date_range(self) -> dict[str, Any]:
        payload = self.load()
        return self._write(self._mark_date_range_completed(payload))

    def update_next_run(self, next_run_utc: Optional[datetime]) -> dict[str, Any]:
        if next_run_utc is None:
            return self.complete_date_range()
        payload = self.load()
        payload["next_run_utc"] = next_run_utc.astimezone(timezone.utc).isoformat()
        return self._write(payload)

    def record_status(self, status: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        status_payload = dict(status)
        status_payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
        payload["last_status"] = status_payload
        return self._write(payload)

    def _write(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload_to_write = self._without_view(payload)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload_to_write, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(self.path)
        return self._with_view(payload_to_write)

    def _mark_date_range_completed(
        self,
        payload: dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        completed_at = now or datetime.now(timezone.utc)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        completed_at = completed_at.astimezone(timezone.utc)
        completed_payload = dict(payload)
        completed_payload["enabled"] = False
        completed_payload["updated_at_utc"] = completed_at.isoformat()
        completed_payload["completed_at_utc"] = completed_at.isoformat()
        completed_payload["next_run_utc"] = None
        if completed_payload.get("schedule_id"):
            completed_payload["last_status"] = {
                "status": "completed",
                "reason": "date_range_completed",
                "schedule_id": completed_payload.get("schedule_id"),
                "schedule_label": completed_payload.get("schedule_label"),
                "completed_at_utc": completed_at.isoformat(),
                "recorded_at_utc": completed_at.isoformat(),
            }
        return completed_payload

    def _without_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean_payload = dict(payload)
        clean_payload.pop("scheduled_warmups", None)
        return clean_payload

    def _with_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        view_payload = self._without_view(payload)
        schedule_id = view_payload.get("schedule_id")
        if not schedule_id:
            view_payload["scheduled_warmups"] = []
            return view_payload

        last_status = view_payload.get("last_status")
        date_range_status = model_warmup_schedule_date_range_status(view_payload)
        view_payload["date_range_status"] = date_range_status
        status = "scheduled" if bool(view_payload.get("enabled")) else "canceled"
        if date_range_status == MODEL_WARMUP_DATE_RANGE_STATUS_COMPLETED:
            status = "completed"
        if not bool(view_payload.get("enabled")) and isinstance(last_status, dict):
            status = str(last_status.get("status") or status)

        view_payload["scheduled_warmups"] = [
            {
                "schedule_id": schedule_id,
                "enabled": bool(view_payload.get("enabled")),
                "status": status,
                "cadence": view_payload.get("cadence"),
                "schedule_label": view_payload.get("schedule_label"),
                "timezone_name": view_payload.get("timezone_name"),
                "start_date": view_payload.get("start_date"),
                "end_date": view_payload.get("end_date"),
                "date_range_status": date_range_status,
                "next_run_utc": view_payload.get("next_run_utc"),
                "canceled_at_utc": view_payload.get("canceled_at_utc"),
                "completed_at_utc": view_payload.get("completed_at_utc"),
                "updated_at_utc": view_payload.get("updated_at_utc"),
                "last_status": last_status,
                "run_request": view_payload.get("run_request") or {},
            }
        ]
        return view_payload


class ModelWarmupScheduler:
    """Daemon scheduler that starts AVA Spec Warm Up runs when due."""

    def __init__(
        self,
        *,
        settings_getter: Callable[[], dict[str, Any]],
        run_job: Callable[[dict[str, Any], datetime], None],
        next_run_updater: Optional[Callable[[datetime], None]] = None,
        completion_handler: Optional[Callable[[], None]] = None,
        poll_interval_seconds: float = 20.0,
    ):
        self.settings_getter = settings_getter
        self.run_job = run_job
        self.next_run_updater = next_run_updater
        self.completion_handler = completion_handler
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_signature: Optional[tuple] = None
        self._next_run_utc: Optional[datetime] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_pending_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval_seconds)

    def _signature(self, settings: dict[str, Any]) -> tuple:
        return (
            bool(settings.get("enabled")),
            str(settings.get("schedule_id") or ""),
            str(settings.get("cadence") or ""),
            str(settings.get("timezone_name") or ""),
            str(settings.get("minute") or ""),
            str(settings.get("time_hhmm") or ""),
            str(settings.get("weekday") or ""),
            str(settings.get("day_of_month") or ""),
            str(settings.get("start_date") or ""),
            str(settings.get("end_date") or ""),
            json.dumps(settings.get("run_request") or {}, sort_keys=True),
        )

    def _complete_date_range(self) -> None:
        self._last_signature = None
        self._next_run_utc = None
        if self.completion_handler is not None:
            self.completion_handler()

    def _run_pending_once(self) -> None:
        settings = self.settings_getter() or {}
        if not bool(settings.get("enabled")):
            self._last_signature = None
            self._next_run_utc = None
            return

        signature = self._signature(settings)
        now = datetime.now(timezone.utc)
        if signature != self._last_signature or self._next_run_utc is None:
            self._next_run_utc = compute_next_model_warmup_run_utc(settings, now_utc=now)
            if self._next_run_utc is None:
                self._complete_date_range()
                return
            self._last_signature = signature
            if self.next_run_updater is not None:
                self.next_run_updater(self._next_run_utc)

        if self._next_run_utc and now >= self._next_run_utc:
            due_at = self._next_run_utc
            self.run_job(settings, due_at)
            self._next_run_utc = compute_next_model_warmup_run_utc(
                settings,
                now_utc=now + timedelta(seconds=1),
            )
            if self._next_run_utc is None:
                self._complete_date_range()
                return
            if self.next_run_updater is not None:
                self.next_run_updater(self._next_run_utc)
