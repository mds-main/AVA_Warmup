"""Warm-up suite specification loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_WARMUP_SUITE_ID = "ava_spec_default"
DEFAULT_WARMUP_SUITE_NAME = "AVA Spec Warm Up Suite"
DEFAULT_WARMUP_SCENARIO_NAME = "No Help Needed Warm Up"
DEFAULT_WARMUP_MESSAGE = "no help needed"


@dataclass(frozen=True)
class WarmupSuiteSpec:
    """Operator-selectable warm-up conversation routine."""

    suite_id: str = DEFAULT_WARMUP_SUITE_ID
    suite_name: str = DEFAULT_WARMUP_SUITE_NAME
    scenario_name: str = DEFAULT_WARMUP_SCENARIO_NAME
    messages: tuple[str, ...] = (DEFAULT_WARMUP_MESSAGE,)
    source_path: str | None = None

    @property
    def first_message(self) -> str:
        return self.messages[0]

    @property
    def message_label(self) -> str:
        if len(self.messages) == 1:
            return self.first_message
        return f"{self.first_message} (+{len(self.messages) - 1} more)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "suite_name": self.suite_name,
            "scenario_name": self.scenario_name,
            "messages": list(self.messages),
        }


DEFAULT_WARMUP_SUITE = WarmupSuiteSpec()


def default_warmup_suite(default_message: str | None = None) -> WarmupSuiteSpec:
    """Build the default suite with an optional first-message override.

    Used to apply ``AVA_WARMUP_DEFAULT_MESSAGE`` (env-driven) without mutating
    the module-level ``DEFAULT_WARMUP_SUITE`` constant.
    """

    message = str(default_message or "").strip() or DEFAULT_WARMUP_MESSAGE
    if message == DEFAULT_WARMUP_MESSAGE:
        return DEFAULT_WARMUP_SUITE
    return WarmupSuiteSpec(messages=(message,))


def apply_first_message_override(suite: WarmupSuiteSpec, override: str | None) -> WarmupSuiteSpec:
    """Return a copy of ``suite`` with ``messages[0]`` replaced by ``override``.

    Whitespace-only or empty overrides leave the suite unchanged. Trailing
    messages (multi-step suites) are preserved.
    """

    normalized = str(override or "").strip()
    if not normalized:
        return suite
    remaining = suite.messages[1:] if len(suite.messages) > 1 else ()
    return WarmupSuiteSpec(
        suite_id=suite.suite_id,
        suite_name=suite.suite_name,
        scenario_name=suite.scenario_name,
        messages=(normalized,) + remaining,
        source_path=suite.source_path,
    )


def normalize_suite_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return DEFAULT_WARMUP_SUITE_ID
    return normalized


def warmup_suites_dir(project_root: Path) -> Path:
    return project_root / "warmup_suites"


def suite_from_dict(payload: dict[str, Any], *, suite_id: str | None = None, source_path: str | None = None) -> WarmupSuiteSpec:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("Warm-up suite messages must be a non-empty list.")
    messages = tuple(str(message or "").strip() for message in raw_messages)
    messages = tuple(message for message in messages if message)
    if not messages:
        raise ValueError("Warm-up suite messages must include at least one non-empty message.")

    suite_name = str(payload.get("suite_name") or "").strip()
    scenario_name = str(payload.get("scenario_name") or "").strip()
    if not suite_name:
        raise ValueError("Warm-up suite suite_name must not be blank.")
    if not scenario_name:
        raise ValueError("Warm-up suite scenario_name must not be blank.")

    resolved_suite_id = normalize_suite_id(
        suite_id or str(payload.get("suite_id") or "").strip() or source_path or DEFAULT_WARMUP_SUITE_ID
    )
    return WarmupSuiteSpec(
        suite_id=resolved_suite_id,
        suite_name=suite_name,
        scenario_name=scenario_name,
        messages=messages,
        source_path=source_path,
    )


def suite_from_request_payload(payload: dict[str, Any] | None) -> WarmupSuiteSpec:
    if not isinstance(payload, dict):
        return DEFAULT_WARMUP_SUITE
    return suite_from_dict(payload, suite_id=str(payload.get("suite_id") or DEFAULT_WARMUP_SUITE_ID))


def load_suite_file(path: Path) -> WarmupSuiteSpec:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return suite_from_dict(payload, suite_id=path.stem, source_path=str(path))


def load_available_suites(project_root: Path) -> tuple[list[WarmupSuiteSpec], list[str]]:
    suites = {DEFAULT_WARMUP_SUITE.suite_id: DEFAULT_WARMUP_SUITE}
    errors: list[str] = []
    directory = warmup_suites_dir(project_root)
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            try:
                suite = load_suite_file(path)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            suites[suite.suite_id] = suite
    return list(suites.values()), errors


def resolve_suite(project_root: Path, suite_id: str | None) -> WarmupSuiteSpec:
    normalized = normalize_suite_id(suite_id or DEFAULT_WARMUP_SUITE_ID)
    if normalized == DEFAULT_WARMUP_SUITE_ID:
        return DEFAULT_WARMUP_SUITE
    path = warmup_suites_dir(project_root) / f"{normalized}.json"
    if not path.exists():
        raise ValueError(f"Warm-up suite '{normalized}' was not found in warmup_suites/.")
    return load_suite_file(path)
