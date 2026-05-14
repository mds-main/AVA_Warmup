"""Focused Flask route tests for the standalone warm-up app."""

import json
from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from ava_warmup.progress import ProgressEmitter
from ava_warmup.runner import (
    MODEL_WARMUP_FIXED_MESSAGE,
    MODEL_WARMUP_SCENARIO_NAME,
    MODEL_WARMUP_SUITE_NAME,
    ModelWarmUpRunRequest,
    build_model_warmup_metadata,
)
from ava_warmup.schemas import (
    AppConfig,
    AttemptResult,
    Message,
    MessageRole,
    ProgressEvent,
    ProgressEventType,
    ScenarioResult,
    TestReport as WarmupTestReport,
)
from ava_warmup.web_app import create_app


class _FakeWarmUpRunner:
    def __init__(self, *, config: AppConfig, progress_emitter: ProgressEmitter, stop_event=None):
        self.config = config
        self.progress_emitter = progress_emitter
        self.stop_event = stop_event

    async def run(self, request: ModelWarmUpRunRequest) -> WarmupTestReport:
        attempts = []
        for attempt_number in range(1, request.attempt_count + 1):
            conversation = [
                Message(role=MessageRole.AGENT, content="Welcome", timestamp=datetime.now(timezone.utc)),
            ]
            for warmup_message in request.suite_spec.messages:
                conversation.extend(
                    [
                        Message(role=MessageRole.USER, content=warmup_message, timestamp=datetime.now(timezone.utc)),
                        Message(role=MessageRole.AGENT, content="Goodbye", timestamp=datetime.now(timezone.utc)),
                    ]
                )
            attempts.append(
                AttemptResult(
                    attempt_number=attempt_number,
                    success=True,
                    conversation=conversation,
                    explanation="AVA Spec Warm Up completed; no judgement performed.",
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    duration_seconds=0.01,
                    warmup_stage_durations_ms={
                        "connect": 1.0,
                        "welcome_wait": 2.0,
                        "agent_response_wait": 3.0,
                        "disconnect": 1.0,
                    },
                )
            )
        scenario = ScenarioResult(
            scenario_name=request.suite_spec.scenario_name,
            attempts=len(attempts),
            successes=len(attempts),
            failures=0,
            timeouts=0,
            skipped=0,
            success_rate=1.0,
            is_regression=False,
            attempt_results=attempts,
        )
        return WarmupTestReport(
            suite_name=request.suite_spec.suite_name,
            timestamp=datetime.now(timezone.utc),
            duration_seconds=0.05,
            scenario_results=[scenario],
            overall_attempts=len(attempts),
            overall_successes=len(attempts),
            overall_failures=0,
            overall_timeouts=0,
            overall_skipped=0,
            overall_success_rate=1.0,
            model_warmup_run=build_model_warmup_metadata(
                request,
                completed_attempts=len(attempts),
                attempts_per_second=20.0,
                duration_percentiles={"p50": 0.01, "p95": 0.01, "p99": 0.01},
                stage_duration_percentiles={
                    "connect": {"p50": 1.0, "p95": 1.0, "p99": 1.0},
                    "agent_response_wait": {"p50": 3.0, "p95": 3.0, "p99": 3.0},
                },
            ),
            has_regressions=False,
            regression_threshold=self.config.success_threshold,
        )


TEST_ADMIN_USER = "admin"
TEST_ADMIN_PASSWORD = "warmup-pass"


def _login(client, username=TEST_ADMIN_USER, password=TEST_ADMIN_PASSWORD):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("ava_warmup.web_app.ModelWarmUpRunner", _FakeWarmUpRunner)
    suite_dir = tmp_path / "warmup_suites"
    suite_dir.mkdir()
    (suite_dir / "custom_support.json").write_text(
        json.dumps(
            {
                "suite_name": "Custom Support Warm Up",
                "scenario_name": "Two Message Check",
                "messages": ["hello", "no help needed"],
            }
        ),
        encoding="utf-8",
    )
    flask_app = create_app(
        AppConfig(
            history_dir=str(tmp_path),
            history_max_runs=10,
            history_full_json_runs=10,
            history_gzip_runs=0,
            admin_user=TEST_ADMIN_USER,
            admin_password=TEST_ADMIN_PASSWORD,
        )
    )
    flask_app.config["warmup_suites_project_root"] = tmp_path
    return flask_app


@pytest.fixture
def client(app):
    test_client = app.test_client()
    _login(test_client)
    return test_client


@pytest.fixture
def anonymous_client(app):
    return app.test_client()


def _wait_until_idle(client, timeout=2.0):
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        last_status = client.get("/run/status").get_json()
        if not last_status["run_active"]:
            return last_status
        time.sleep(0.02)
    raise AssertionError(f"run did not finish: {last_status}")


def test_home_renders_warmup_only_form(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "AVA Spec Warm Up" in body
    assert "Custom Support Warm Up" in body
    assert "Selected Suite Details" in body
    assert "Suite Builder" not in body
    assert "Transcript" not in body


def test_run_model_warm_up_json_completes_and_persists_history(client):
    response = client.post(
        "/run/model_warm_up",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 2,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
        },
    )
    assert response.status_code == 202
    assert response.get_json()["ok"] is True

    _wait_until_idle(client)

    history = client.get("/results/history").get_json()["runs"]
    assert len(history) == 1
    assert history[0]["run_type"] == "model_warm_up"
    assert history[0]["model_warmup_run"]["completed_attempts"] == 2
    # /results/history must surface duration_seconds so the SPA history table
    # can render the Duration column for past runs.
    assert "duration_seconds" in history[0]
    assert history[0]["duration_seconds"] is not None

    results = client.get("/results")
    assert results.status_code == 200
    results_body = results.get_data(as_text=True)
    assert "AVA Spec Warm Up Performance" in results_body
    assert "Performance, schedule, and local history for AVA Spec Warm Up runs." in results_body
    assert "Completed Attempts" in results_body
    assert "<details class=\"card attempts-card attempts-section\" data-live-attempts=\"true\" open>" in results_body
    assert "<details class=\"attempt-row ok\">" in results_body
    assert "Attempt 1" in results_body
    assert "Web Messenger Interaction Snapshot" in results_body
    assert "Welcome" in results_body
    assert MODEL_WARMUP_FIXED_MESSAGE in results_body
    assert "Back to Top" in results_body


def test_run_model_warm_up_uses_selected_custom_suite(client):
    response = client.post(
        "/run/model_warm_up",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 1,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
            "suite_id": "custom_support",
        },
    )
    assert response.status_code == 202

    _wait_until_idle(client)

    exported = client.get("/results/export?format=json").get_json()
    warmup = exported["model_warmup_run"]
    assert exported["suite_name"] == "Custom Support Warm Up"
    assert warmup["suite_name"] == "Custom Support Warm Up"
    assert warmup["scenario_name"] == "Two Message Check"
    assert warmup["fixed_message"] == "hello"
    assert warmup["warmup_messages"] == ["hello", "no help needed"]


def test_completed_attempts_section_collapses_while_run_active(app, client):
    emitter = ProgressEmitter()
    run_request = ModelWarmUpRunRequest(
        deployment_id="deploy-123",
        region="usw2.pure.cloud",
        attempt_count=2,
    )
    with app.config["run_state_lock"]:
        app.config["run_active"] = True
        app.config["active_run_id"] = "run-123"
        app.config["active_run_type"] = "model_warm_up"
        app.config["progress_emitter"] = emitter
        app.config["active_model_warmup_metadata"] = build_model_warmup_metadata(run_request)

    results_body = client.get("/results").get_data(as_text=True)

    assert "class=\"card attempts-card attempts-section\"" in results_body
    assert "data-live-attempts=\"true\"" in results_body
    assert "<details class=\"card attempts-card attempts-section\" data-live-attempts=\"true\" open>" not in results_body


def test_run_model_warm_up_validation_error(client):
    response = client.post("/run/model_warm_up", json={"deployment_id": ""})

    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert "Deployment ID is required for AVA Spec Warm Up." in errors
    assert "Region is required for AVA Spec Warm Up." in errors


def test_schedule_save_status_and_cancel(client):
    response = client.post(
        "/run/model_warm_up/schedule",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 3,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
            "cadence": "daily",
            "timezone_name": "UTC",
            "time_hhmm": "02:00",
            "start_date": "2099-04-27",
            "end_date": "2099-04-30",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["schedule"]["enabled"] is True

    status = client.get("/run/model_warm_up/schedule/status").get_json()
    assert status["scheduled_warmups"][0]["run_request"]["attempt_count"] == 3
    assert status["scheduled_warmups"][0]["run_request"]["suite_spec"]["suite_name"] == "AVA Spec Warm Up Suite"

    cancel = client.post("/run/model_warm_up/schedule/cancel", json={})
    assert cancel.status_code == 200
    assert cancel.get_json()["schedule"]["enabled"] is False


def test_schedule_persists_selected_custom_suite(client):
    response = client.post(
        "/run/model_warm_up/schedule",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 3,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
            "suite_id": "custom_support",
            "cadence": "daily",
            "timezone_name": "UTC",
            "time_hhmm": "02:00",
            "start_date": "2099-04-27",
            "end_date": "2099-04-30",
        },
    )

    assert response.status_code == 200
    run_request = response.get_json()["schedule"]["run_request"]
    assert run_request["suite_id"] == "custom_support"
    assert run_request["suite_spec"]["suite_name"] == "Custom Support Warm Up"
    assert run_request["suite_spec"]["messages"] == ["hello", "no help needed"]


def test_malformed_selected_suite_returns_validation_error(app, client, tmp_path):
    (tmp_path / "warmup_suites" / "broken.json").write_text(
        '{"suite_name": "Broken", "scenario_name": "Broken"}',
        encoding="utf-8",
    )

    response = client.post(
        "/run/model_warm_up",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 1,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
            "suite_id": "broken",
        },
    )

    assert response.status_code == 400
    assert "messages must be a non-empty list" in response.get_json()["errors"][0]


def test_results_export_json_and_csv(client):
    client.post(
        "/run/model_warm_up",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 1,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
        },
    )
    _wait_until_idle(client)

    json_response = client.get("/results/export?format=json")
    assert json_response.status_code == 200
    assert json_response.get_json()["model_warmup_run"]["completed_attempts"] == 1

    csv_response = client.get("/results/export?format=csv")
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["Content-Type"]
    assert "attempts_per_second" in csv_response.get_data(as_text=True)


def test_results_export_png_uses_dashboard_capture(app, client):
    client.post(
        "/run/model_warm_up",
        json={
            "deployment_id": "deploy-123",
            "region": "usw2.pure.cloud",
            "attempt_count": 1,
            "execution_mode": "serial",
            "pacing_seconds": 1.0,
        },
    )
    _wait_until_idle(client)
    captured_calls = []

    def fake_capture(url, selector):
        captured_calls.append((url, selector))
        return b"\x89PNG\r\n\x1a\nfake"

    app.config["results_png_capture"] = fake_capture

    png_response = client.get("/results/export?format=png")

    assert png_response.status_code == 200
    assert png_response.headers["Content-Type"] == "image/png"
    assert png_response.data.startswith(b"\x89PNG")
    assert captured_calls
    assert "screenshot=1" in captured_calls[0][0]
    assert captured_calls[0][1] == "#results-performance-card"


def test_run_status_derives_live_progress_snapshot(app, client):
    emitter = ProgressEmitter()
    run_request = ModelWarmUpRunRequest(
        deployment_id="deploy-123",
        region="usw2.pure.cloud",
        attempt_count=4,
    )
    emitter.emit(
        ProgressEvent(
            event_type=ProgressEventType.SUITE_STARTED,
            suite_name=MODEL_WARMUP_SUITE_NAME,
            message="Starting suite",
            planned_attempts=4,
            completed_attempts=0,
        )
    )
    emitter.emit(
        ProgressEvent(
            event_type=ProgressEventType.ATTEMPT_COMPLETED,
            suite_name=MODEL_WARMUP_SUITE_NAME,
            scenario_name=MODEL_WARMUP_SCENARIO_NAME,
            attempt_number=2,
            success=True,
            message="Attempt 2: success (2/4)",
            planned_attempts=4,
            completed_attempts=2,
        )
    )
    with app.config["run_state_lock"]:
        app.config["run_active"] = True
        app.config["active_run_id"] = "run-123"
        app.config["active_run_type"] = "model_warm_up"
        app.config["progress_emitter"] = emitter
        app.config["active_model_warmup_metadata"] = build_model_warmup_metadata(run_request)

    payload = client.get("/run/status").get_json()
    results_body = client.get("/results").get_data(as_text=True)

    assert payload["model_warmup_run"]["completed_attempts"] == 2
    assert payload["live_progress"]["planned_attempts"] == 4
    assert payload["live_progress"]["completed_attempts"] == 2
    assert payload["live_progress"]["remaining_attempts"] == 2
    assert payload["live_progress"]["percent_complete"] == 50.0
    assert payload["live_progress"]["estimated_remaining_seconds"] is not None
    assert "Diagnostics Live View" in results_body
    assert "Starting suite" in results_body


def test_bootstrap_json_escapes_script_breakout(app, client):
    """Bootstrap JSON must escape <, >, and & so values cannot terminate
    the surrounding <script type="application/json"> block. Inject a value
    containing </script> via a custom suite to prove the escape works on
    user-controlled content too."""

    body = client.get("/").get_data(as_text=True)
    # Pull out the <script id="ava-bootstrap"> payload.
    start_marker = '<script id="ava-bootstrap" type="application/json">'
    end_marker = "</script>"
    start = body.index(start_marker) + len(start_marker)
    end = body.index(end_marker, start)
    payload = body[start:end]
    # The escape replaces all `<`, `>`, `&` with their unicode-escape form, so
    # the literal characters never appear inside the JSON payload.
    assert "<" not in payload
    assert ">" not in payload
    assert "&" not in payload
    # Sanity check: the payload is still valid JSON after the substitutions and
    # the escaped sequences round-trip back to the original characters.
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)

    # Inject a value that *would* break out if the escape ever regressed: place a
    # </script> in a custom warm-up suite file. Render the page again and confirm
    # the script tag survives intact.
    suite_path = (
        Path(app.config["warmup_suites_project_root"])
        / "warmup_suites"
        / "xss_probe.json"
    )
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "XSS </script><img src=x onerror=alert(1)>",
                "scenario_name": "probe",
                "messages": ["hello"],
            }
        ),
        encoding="utf-8",
    )
    body2 = client.get("/").get_data(as_text=True)
    start2 = body2.index(start_marker) + len(start_marker)
    end2 = body2.index(end_marker, start2)
    probed = body2[start2:end2]
    assert "</script>" not in probed
    assert "<" not in probed and ">" not in probed
    # And the suite name still round-trips through JSON parsing.
    parsed2 = json.loads(probed)
    suite_names = {s["suite_name"] for s in parsed2.get("suites", [])}
    assert "XSS </script><img src=x onerror=alert(1)>" in suite_names


def test_env_driven_defaults_flow_into_home_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr("ava_warmup.web_app.ModelWarmUpRunner", _FakeWarmUpRunner)
    flask_app = create_app(
        AppConfig(
            history_dir=str(tmp_path),
            history_max_runs=10,
            history_full_json_runs=10,
            history_gzip_runs=0,
            default_attempt_count=42,
            default_execution_mode="parallel",
            default_worker_count=3,
            default_pacing_seconds=2.5,
            default_cadence="hourly",
            default_minute=17,
            default_timezone="America/New_York",
            default_schedule_end_date="2099-01-31",
            admin_user=TEST_ADMIN_USER,
            admin_password=TEST_ADMIN_PASSWORD,
        )
    )
    flask_app.config["warmup_suites_project_root"] = tmp_path

    test_client = flask_app.test_client()
    _login(test_client)
    body = test_client.get("/").get_data(as_text=True)

    assert '"default_attempt_count": 42' in body
    assert '"default_execution_mode": "parallel"' in body
    assert '"default_worker_count": 3' in body
    assert '"default_pacing_seconds": 2.5' in body
    assert '"default_cadence": "hourly"' in body
    assert '"default_minute": 17' in body
    assert '"default_timezone": "America/New_York"' in body
    assert '"default_schedule_end_date": "2099-01-31"' in body


def test_auto_schedule_bootstrap_writes_schedule_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr("ava_warmup.web_app.ModelWarmUpRunner", _FakeWarmUpRunner)
    flask_app = create_app(
        AppConfig(
            history_dir=str(tmp_path),
            history_max_runs=10,
            history_full_json_runs=10,
            history_gzip_runs=0,
            gc_deployment_id="env-deploy-id",
            gc_region="usw2.pure.cloud",
            default_attempt_count=228,
            default_cadence="hourly",
            default_minute=0,
            default_timezone="UTC",
            default_schedule_end_date="2099-12-31",
            auto_schedule_enabled=True,
            admin_user=TEST_ADMIN_USER,
            admin_password=TEST_ADMIN_PASSWORD,
        )
    )
    flask_app.config["warmup_suites_project_root"] = tmp_path

    test_client = flask_app.test_client()
    _login(test_client)
    status = test_client.get("/run/model_warm_up/schedule/status").get_json()

    scheduled = status["scheduled_warmups"]
    assert scheduled
    assert scheduled[0]["enabled"] is True
    assert scheduled[0]["cadence"] == "hourly"
    assert scheduled[0]["status"] == "scheduled"
    run_request = scheduled[0]["run_request"]
    assert run_request["deployment_id"] == "env-deploy-id"
    assert run_request["region"] == "usw2.pure.cloud"
    assert run_request["attempt_count"] == 228


def test_auto_schedule_bootstrap_disabled_leaves_no_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr("ava_warmup.web_app.ModelWarmUpRunner", _FakeWarmUpRunner)
    flask_app = create_app(
        AppConfig(
            history_dir=str(tmp_path),
            history_max_runs=10,
            history_full_json_runs=10,
            history_gzip_runs=0,
            gc_deployment_id="env-deploy-id",
            gc_region="usw2.pure.cloud",
            default_schedule_end_date="2099-12-31",
            auto_schedule_enabled=False,
            admin_user=TEST_ADMIN_USER,
            admin_password=TEST_ADMIN_PASSWORD,
        )
    )
    flask_app.config["warmup_suites_project_root"] = tmp_path

    test_client = flask_app.test_client()
    _login(test_client)
    status = test_client.get("/run/model_warm_up/schedule/status").get_json()

    assert status.get("scheduled_warmups") == []


def test_results_show_failure_diagnostics_and_csv_summary(app, client):
    failed_attempt = AttemptResult(
        attempt_number=1,
        success=False,
        conversation=[],
        explanation="AVA Spec Warm Up attempt failed due to Web Messaging error.",
        error="Failed to connect to Web Messaging API: connecting through a SOCKS proxy requires python-socks",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=0.01,
    )
    scenario = ScenarioResult(
        scenario_name=MODEL_WARMUP_SCENARIO_NAME,
        attempts=1,
        successes=0,
        failures=1,
        timeouts=0,
        skipped=0,
        success_rate=0.0,
        is_regression=True,
        attempt_results=[failed_attempt],
    )
    run_request = ModelWarmUpRunRequest(
        deployment_id="deploy-123",
        region="usw2.pure.cloud",
        attempt_count=1,
    )
    app.config["latest_report"] = WarmupTestReport(
        suite_name=MODEL_WARMUP_SUITE_NAME,
        timestamp=datetime.now(timezone.utc),
        duration_seconds=0.01,
        scenario_results=[scenario],
        overall_attempts=1,
        overall_successes=0,
        overall_failures=1,
        overall_timeouts=0,
        overall_skipped=0,
        overall_success_rate=0.0,
        model_warmup_run=build_model_warmup_metadata(run_request, completed_attempts=1),
        has_regressions=True,
        regression_threshold=app.config["app_config"].success_threshold,
    )

    results_body = client.get("/results").get_data(as_text=True)
    csv_body = client.get("/results/export?format=csv").get_data(as_text=True)

    assert "Failure Diagnostics" in results_body
    assert "python-socks" in results_body
    assert "failure_summary" in csv_body
    assert "python-socks" in csv_body


def test_unauthenticated_home_redirects_to_login(anonymous_client):
    response = anonymous_client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_unauthenticated_protected_routes_redirect_to_login(anonymous_client):
    for path in ("/results", "/results/history", "/run/model_warm_up/schedule/status"):
        response = anonymous_client.get(path, follow_redirects=False)
        assert response.status_code == 302, f"{path} should redirect when unauthenticated"
        assert "/login" in response.headers["Location"]


def test_unauthenticated_json_request_returns_401(anonymous_client):
    response = anonymous_client.post(
        "/run/model_warm_up",
        json={"deployment_id": "deploy-123", "region": "usw2.pure.cloud"},
    )

    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_login_get_renders_login_form(anonymous_client):
    response = anonymous_client.get("/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Administrator sign in" in body
    assert 'name="username"' in body
    assert 'name="password"' in body


def test_login_with_wrong_credentials_shows_error(anonymous_client):
    response = anonymous_client.post(
        "/login",
        data={"username": TEST_ADMIN_USER, "password": "wrong"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Invalid username or password." in response.get_data(as_text=True)


def test_login_with_valid_credentials_grants_access(anonymous_client):
    response = _login(anonymous_client)

    assert response.status_code == 302
    assert "/login" not in response.headers["Location"]

    home_response = anonymous_client.get("/")
    assert home_response.status_code == 200
    assert "AVA Spec Warm Up" in home_response.get_data(as_text=True)


def test_login_redirects_to_safe_next_path(anonymous_client):
    response = anonymous_client.post(
        "/login",
        data={
            "username": TEST_ADMIN_USER,
            "password": TEST_ADMIN_PASSWORD,
            "next": "/results",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/results")


def test_login_rejects_unsafe_next_path(anonymous_client):
    response = anonymous_client.post(
        "/login",
        data={
            "username": TEST_ADMIN_USER,
            "password": TEST_ADMIN_PASSWORD,
            "next": "https://evil.example.com/steal",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_logout_clears_session(client):
    assert client.get("/").status_code == 200

    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 302
    assert "/login" in logout_response.headers["Location"]

    after_logout = client.get("/", follow_redirects=False)
    assert after_logout.status_code == 302
    assert "/login" in after_logout.headers["Location"]


def test_static_assets_accessible_without_login(anonymous_client):
    response = anonymous_client.get("/static/css/app.css")
    assert response.status_code == 200


def test_healthz_accessible_without_login(anonymous_client):
    response = anonymous_client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_app_returns_503_when_admin_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("ava_warmup.web_app.ModelWarmUpRunner", _FakeWarmUpRunner)
    flask_app = create_app(
        AppConfig(
            history_dir=str(tmp_path),
            history_max_runs=10,
            history_full_json_runs=10,
            history_gzip_runs=0,
        )
    )
    flask_app.config["warmup_suites_project_root"] = tmp_path

    response = flask_app.test_client().get("/")
    assert response.status_code == 503
    assert b"Authentication is not configured" in response.data
