# AVA Spec Warm Up

Standalone Flask application for warming up a Genesys Cloud AVA/Web Messaging deployment. It opens Web Messaging conversations, sends a default or custom warm-up message sequence, records transport timing metrics, and uses a locally installed LLM for the warm-up workflow.

## What It Does

- Runs the default suite `AVA Spec Warm Up Suite`.
- Runs the default scenario `No Help Needed Warm Up`.
- Sends `no help needed` for every default-suite attempt, with optional custom JSON suites under `warmup_suites/`.
- Uses a local Ollama model. `gemma4:e4b` is recommended because this application has been optimized for it.
- Captures Web Messaging transport success, timeout, failure, latency, stage timing, and compact diagnostics.
- Supports manual runs, one persistent local schedule, live status, stop requests, local history, and JSON/CSV/PNG exports.

## Quick Start

Run these commands from the repository root. CI validates Python `3.9` and `3.11`; use Python `3.9` or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Install the local LLM with Ollama before running warm-ups. On macOS, install Ollama with Homebrew or from the Ollama desktop installer:

```bash
brew install ollama
```

Start Ollama if it is not already running:

```bash
ollama serve
```

In a separate terminal, pull the recommended model:

```bash
ollama pull gemma4:e4b
```

Verify the model is installed and responding:

```bash
ollama list
ollama run gemma4:e4b "Respond with ready."
```

Keep Ollama running while you use the warm-up app. In the web form, use `gemma4:e4b` as the LLM model label unless you intentionally test a different local model.

PNG export uses Playwright Chromium. Install the browser once if you want `Export PNG` to work:

```bash
python3 -m playwright install chromium
```

Start the web app:

```bash
python3 -m ava_warmup
```

Open `http://localhost:8080`. The app binds to `0.0.0.0` and uses the `PORT` env var (or `AVA_WARMUP_PORT`, default `8080`) so the same entrypoint runs locally and on PaaS hosts. Flask debug mode is disabled. For production use gunicorn (see the Deployment section); the bare `python -m ava_warmup` command uses the Flask dev server and is for local use only.

## Running A Warm-Up

Use the `Run` page to start a manual warm-up. Deployment ID and region are required, either from the form or environment defaults. Enter `gemma4:e4b` in the LLM Model field after installing it locally with Ollama. Choose a warm-up suite from the selector; the default suite remains selected unless you choose a custom suite file.

Run controls:

- `Attempt Count`: defaults to `228`; must be at least `1`.
- `Execution Mode`: `serial` runs one attempt at a time; `parallel` runs multiple workers.
- `Parallel Workers`: used only for parallel mode; allowed range is `1` to `5`.
- `Pacing`: allowed values are `0.5`, `1.0`, `2.5`, `5.0`, and `7.5` seconds.
- `Performance Profile`: only `safe_adaptive` is supported. It reduces effective worker or pacing pressure when timeout or error pressure rises.
- `Warm-Up Suite`: selects the built-in default or a custom JSON suite saved under `warmup_suites/`.

After a run starts, the `Results` page shows live progress and polls `/run/status`. You can request a cooperative stop with `Stop Run`. Completed and in-progress results include success rate, attempts/sec, timeout and failure counts, duration percentiles, per-stage Web Messaging percentiles, live diagnostics, completed attempt details, adaptive adjustments, schedule status, and local run history.

## Designing Custom Warm-Up Suites

Custom warm-up suites are JSON files saved in the repository’s `warmup_suites/` directory. The filename becomes the selectable suite id, so `warmup_suites/custom_support.json` is selected with `suite_id=custom_support` in the API. The Run page loads this directory whenever it renders, lists valid suites in the `Warm-Up Suite` selector, and shows validation errors for malformed suite files without blocking valid suites.

Each file uses this structure:

```json
{
  "suite_name": "Custom Support Warm Up",
  "scenario_name": "Two Message Check",
  "messages": [
    "hello",
    "no help needed"
  ]
}
```

Fields:

- `suite_name`: display and report name for the suite.
- `scenario_name`: scenario name shown in reports and progress events.
- `messages`: ordered list of non-empty user messages sent during each attempt.

The checked-in `warmup_suites/ava_spec_default.json` mirrors the built-in default, and `warmup_suites/example_custom.json` shows a simple two-message custom routine. Each attempt sends every message in the selected suite in order and captures the Web Messenger interaction snapshot in results. Custom schedules persist the full selected suite spec, so a saved schedule keeps using the same suite name, scenario name, and message sequence even if the source JSON file later changes.

## Scheduling

The `Run` page can save the current run configuration as one persistent automatic schedule. Manual runs remain separate from scheduled runs.

Supported schedule cadences:

- `hourly`: runs at the selected minute of each hour.
- `daily`: runs at the selected local time.
- `weekly`: runs on the selected weekday and local time.
- `monthly`: runs on the selected day of month and local time. Months with fewer days use the last valid day of that month.
- Positive integer N (via `AVA_WARMUP_DEFAULT_CADENCE`): runs every N hours at the selected minute, anchored to the schedule's start date at `00:MM` local time.

Schedules use an IANA timezone such as `UTC` or `America/New_York`. The start date defaults to the current local date when blank, and the end date is required. Schedule state is stored locally in `model_warmup_schedule.json` under the history directory.

## Local Data

By default, local data is written to `.ava_warmup_history/`.

- `index.json`: local run history index.
- `runs/`: full JSON reports for recent runs.
- `model_warmup_schedule.json`: persistent schedule state.

History retention defaults to `50` runs. The newest `20` reports stay as full JSON, the next `20` are compressed as gzip JSON, and older retained entries become summary-only. The checked-in `warmup_suites/` directory is suite configuration, not run history. Do not commit `.ava_warmup_history/`, `.env`, `config.yaml`, deployment IDs, raw transcripts, or customer conversation artifacts.

## Environment Variables

All configuration is environment-driven. The form pre-fills from env, and (when auto-schedule is enabled) the persistent schedule is reapplied from env on every app boot.

### Mandatory

The app will not serve any request (other than `/healthz`, `/login`, `/logout`, and static assets) until **both** of these are set. Until then, every request returns `503 Authentication is not configured`. Credentials are compared with `hmac.compare_digest` (constant time), and a successful login stores `authenticated=True` plus the username in the Flask session.

| Variable | Purpose |
| --- | --- |
| `ADMIN_USER` | Login username for the web UI. Must be set together with `ADMIN_PASSWORD`. |
| `ADMIN_PASSWORD` | Login password for the web UI. Must be set together with `ADMIN_USER`. |

### Conditionally mandatory

Required only when the feature in the right column is used.

| Variable | Required when | Purpose |
| --- | --- | --- |
| `AVA_WARMUP_DEPLOYMENT_ID` *(alias `GC_DEPLOYMENT_ID`)* | Running a warm-up without typing it in the form, or when `AVA_WARMUP_AUTO_SCHEDULE_ENABLED=true`. | Genesys Cloud Web Messaging deployment ID. |
| `AVA_WARMUP_REGION` *(alias `GC_REGION`)* | Same as above. | Genesys Cloud region, e.g. `mypurecloud.com` or `usw2.pure.cloud`. |
| `AVA_WARMUP_DEFAULT_SCHEDULE_END_DATE` | `AVA_WARMUP_AUTO_SCHEDULE_ENABLED=true`. | `YYYY-MM-DD` end date for the auto-bootstrapped schedule. |

### Optional — Run defaults

| Variable | Default | Notes |
| --- | --- | --- |
| `AVA_WARMUP_DEFAULT_ATTEMPT_COUNT` | `228` | Attempts per run. Must be ≥ 1. |
| `AVA_WARMUP_DEFAULT_EXECUTION_MODE` | `serial` | `serial` or `parallel`. |
| `AVA_WARMUP_DEFAULT_WORKER_COUNT` | `1` | Parallel workers, clamped `1..5`. |
| `AVA_WARMUP_DEFAULT_PACING_SECONDS` | `1.0` | Must be one of `0.5`, `1.0`, `2.5`, `5.0`, `7.5`. |
| `AVA_WARMUP_DEFAULT_PERFORMANCE_PROFILE` | `safe_adaptive` | Only `safe_adaptive` is supported. |

### Optional — Schedule defaults

| Variable | Default | Notes |
| --- | --- | --- |
| `AVA_WARMUP_DEFAULT_CADENCE` | `hourly` | `hourly`, `daily`, `weekly`, `monthly`, or a positive integer N (run every N hours, e.g. `3` fires every 3 hours). |
| `AVA_WARMUP_DEFAULT_MINUTE` | `0` | Minute (`0..59`) for `hourly` and numeric (every-N-hours) cadences. |
| `AVA_WARMUP_DEFAULT_TIME_HHMM` | `02:00` | `HH:MM` local time for `daily`/`weekly`/`monthly`. |
| `AVA_WARMUP_DEFAULT_WEEKDAY` | `0` | `0=Mon` .. `6=Sun` for `weekly`. |
| `AVA_WARMUP_DEFAULT_DAY_OF_MONTH` | `1` | `1..31` for `monthly`. |
| `AVA_WARMUP_DEFAULT_TIMEZONE` | `UTC` | IANA timezone, e.g. `America/New_York`. |
| `AVA_WARMUP_DEFAULT_SCHEDULE_START_DATE` | _(empty)_ | Optional `YYYY-MM-DD` start date. |
| `AVA_WARMUP_AUTO_SCHEDULE_ENABLED` | `false` | When `true`, the persistent schedule is (re)written from env on every boot. Required for ephemeral filesystems like DigitalOcean App Platform. |

### Optional — Runtime, auth, and storage

| Variable | Default | Notes |
| --- | --- | --- |
| `PORT` | `8080` | HTTP port. App Platform/Heroku set this automatically. Wins over `AVA_WARMUP_PORT`. |
| `AVA_WARMUP_PORT` | `8080` | Fallback port when `PORT` is unset. |
| `HOST` | `0.0.0.0` | Bind address. Wins over `AVA_WARMUP_HOST`. |
| `AVA_WARMUP_HOST` | `0.0.0.0` | Fallback bind address when `HOST` is unset. |
| `SESSION_SECRET_KEY` | _(derived)_ | Flask session-cookie signing key. Resolution order: (1) use this value if set; (2) otherwise, if `ADMIN_PASSWORD` is set, derive `sha256("ava-warmup-session::<ADMIN_USER>::<ADMIN_PASSWORD>")` so sessions survive restarts as long as the password is unchanged; (3) otherwise a random per-process key (any restart invalidates all sessions). Set this explicitly in production — it lets you rotate `ADMIN_PASSWORD` without logging every user out, and keeps the signing key out of any password-rotation flow. |
| `AVA_WARMUP_RESPONSE_TIMEOUT` | `90` | Per-stage Web Messaging timeout in seconds. |
| `AVA_WARMUP_SUCCESS_THRESHOLD` | `0.8` | Regression threshold for completion rate; clamped to `0.0..1.0`. |
| `AVA_WARMUP_PERFORMANCE_DIAGNOSTICS_ENABLED` | `true` | Include compact performance diagnostics in reports. |
| `AVA_WARMUP_DEBUG_CAPTURE_FRAMES` | `false` | Capture debug WebSocket frames per attempt. |
| `AVA_WARMUP_DEBUG_FRAME_LIMIT` | `8` | Maximum debug frames per attempt. |
| `AVA_WARMUP_HISTORY_DIR` | `.ava_warmup_history` | Local run and schedule history directory. |
| `AVA_WARMUP_USE_TMP_HISTORY` | `false` | When `true` and `AVA_WARMUP_HISTORY_DIR` is unset, store history under `/tmp/ava_warmup_history`. Recommended for App Platform. |
| `GC_TESTER_HISTORY_DIR` | _(unset)_ | Compatibility fallback for `AVA_WARMUP_HISTORY_DIR`. |
| `AVA_WARMUP_HISTORY_MAX_RUNS` | `50` | Maximum retained history entries. |
| `AVA_WARMUP_HISTORY_FULL_JSON_RUNS` | `20` | Newest reports kept as full JSON. |
| `AVA_WARMUP_HISTORY_GZIP_RUNS` | `20` | Additional reports kept as gzip JSON before summary-only compaction. |

### Example

```bash
# Mandatory
export ADMIN_USER="admin"
export ADMIN_PASSWORD="change-me"

# Genesys Cloud target (mandatory in practice)
export AVA_WARMUP_DEPLOYMENT_ID="your-deployment-id"
export AVA_WARMUP_REGION="mypurecloud.com"

# Override the "228 every hour" defaults if needed
export AVA_WARMUP_DEFAULT_ATTEMPT_COUNT=228
export AVA_WARMUP_DEFAULT_CADENCE=hourly
export AVA_WARMUP_DEFAULT_MINUTE=0

python3 -m ava_warmup
```

## Deploying to DigitalOcean App Platform

This repo ships with the deployment artifacts App Platform looks for:

- `requirements.txt`: pinned dependencies, including `gunicorn`.
- `runtime.txt`: pins Python `3.11.9`.
- `Procfile`: production launch command using gunicorn.
- `.do/app.yaml`: full app spec with env vars, health check, and instance sizing.

Two ways to deploy:

1. **Control panel** — Create a new app, point it at your fork of this repo, then under *Settings → Edit App Spec* paste in `.do/app.yaml` (or let App Platform autodetect from `Procfile` + `runtime.txt`).
2. **doctl** — `doctl apps create --spec .do/app.yaml` (then `doctl apps update <app-id> --spec .do/app.yaml` for changes).

After the first deploy, set these env vars in the App Platform UI (or edit them in `.do/app.yaml`). See [Environment Variables](#environment-variables) above for the full table.

Mandatory:

- `ADMIN_USER` — login username for the web UI (mark the env var as a *secret* in the UI).
- `ADMIN_PASSWORD` — login password for the web UI (mark as *secret*).
- `AVA_WARMUP_DEPLOYMENT_ID` — your Genesys Cloud Web Messaging deployment ID.
- `AVA_WARMUP_REGION` — e.g. `mypurecloud.com`.

For auto-bootstrap of the persistent schedule:

- `AVA_WARMUP_DEFAULT_SCHEDULE_END_DATE` — e.g. `2099-12-31`.
- `AVA_WARMUP_AUTO_SCHEDULE_ENABLED=true` — so the schedule is re-applied from env on every boot.

Recommended:

- `SESSION_SECRET_KEY` — a long random string, marked as *secret*. Keeps sessions valid across deploys when `ADMIN_PASSWORD` rotates.

**Why one instance and one gunicorn worker?** The scheduler runs as a daemon thread inside the worker process and active run state lives in Flask `app.config` (in-memory). Multiple workers or instances would fire each scheduled run multiple times and split run state. We use threads (`--threads 8`) for HTTP concurrency within the single worker instead.

**Ephemeral storage.** App Platform containers do not have persistent disk. Run history (`.ava_warmup_history/` by default, `/tmp/ava_warmup_history` when `AVA_WARMUP_USE_TMP_HISTORY=true`) and the saved schedule do not survive redeploys. With `AVA_WARMUP_AUTO_SCHEDULE_ENABLED=true` the schedule is reapplied from env on every boot, so an hourly warm-up resumes automatically after a redeploy. To persist history long-term, forward run reports to external storage (e.g. download via `/results/export?format=json`).

**Playwright PNG export.** The default Python buildpack does not include Chromium. PNG export will fail on App Platform unless you switch to a Dockerfile-based deploy that installs Chromium and runs `python -m playwright install chromium` during build. JSON and CSV exports work out of the box.

## HTTP API

All run and schedule endpoints accept form data. Endpoints that receive JSON, or requests with `Accept: application/json`, return JSON responses.

- `GET /`: render the run and schedule page.
- `POST /run/model_warm_up`: start a background run. Accepts `deployment_id`, `region`, optional `recorded_model`, `attempt_count`, `execution_mode`, `worker_count`, `pacing_seconds`, `performance_profile`, and `suite_id`. JSON success returns `202` with `{ok, run_id, results_url}`. Validation errors return `400`; an active run returns `409`.
- `GET /run/status`: return active run state, trigger source, stop state, warm-up metadata, live progress, and recent progress events.
- `POST /run/stop`: request that the active run stop. JSON success returns `{ok, stop_requested}`; no active run returns `409`.
- `POST /run/model_warm_up/schedule`: save and enable the persistent schedule. Accepts the run fields plus schedule fields such as `cadence`, `timezone_name`, `start_date`, `end_date`, `minute`, `time_hhmm`, `weekday`, and `day_of_month`.
- `POST /run/model_warm_up/schedule/cancel`: cancel the schedule.
- `POST /run/model_warm_up/schedule/disable`: cancel the schedule; equivalent to `/cancel`.
- `GET /run/model_warm_up/schedule/status`: return persisted schedule status.
- `GET /results`: render the metrics dashboard. Use `?history_run_id=<run_id>` to view a specific retained history run.
- `GET /results/history?limit=100`: return local warm-up run history, including suite name, scenario name, and selected warm-up messages. `limit` is clamped to `1..100`.
- `GET /results/export?format=json`: export the latest, in-progress, or selected report as JSON, including suite/scenario/message metadata.
- `GET /results/export?format=csv`: export a compact metrics CSV named `ava_spec_warm_up_metrics.csv`, including suite name, scenario name, fixed message, and ordered warm-up messages.
- `GET /results/export?format=png`: export the results performance card as `ava_spec_warm_up_results.png`. Requires Playwright Chromium.

For exports, add `history_run_id=<run_id>` to export a specific retained history report.

## Testing

Run validation from the repository root:

```bash
python3 -m compileall ava_warmup tests
python3 -m pytest tests/test_runner.py tests/test_scheduler.py tests/test_web_app.py -q
python3 -m pytest -q
```

The tests use fake Web Messaging clients and temporary history directories; they do not connect to Genesys Cloud.
