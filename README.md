# AVA Spec Warm Up

Standalone Flask application for warming up a Genesys Cloud AVA/Web Messaging deployment. It opens Web Messaging conversations, sends the fixed message `no help needed`, records transport timing metrics, and uses a locally installed LLM for the warm-up workflow.

## What It Does

- Runs the fixed suite `AVA Spec Warm Up Suite`.
- Runs the fixed scenario `No Help Needed Warm Up`.
- Sends exactly `no help needed` for every attempt.
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

Open `http://localhost:5000`. The app runs on `0.0.0.0:5000` with Flask debug mode disabled.

## Running A Warm-Up

Use the `Run` page to start a manual warm-up. Deployment ID and region are required, either from the form or environment defaults. Enter `gemma4:e4b` in the LLM model label field after installing it locally with Ollama.

Run controls:

- `Attempt Count`: defaults to `228`; must be at least `1`.
- `Execution Mode`: `serial` runs one attempt at a time; `parallel` runs multiple workers.
- `Parallel Workers`: used only for parallel mode; allowed range is `1` to `5`.
- `Pacing`: allowed values are `0.5`, `1.0`, `2.5`, `5.0`, and `7.5` seconds.
- `Performance Profile`: only `safe_adaptive` is supported. It reduces effective worker or pacing pressure when timeout or error pressure rises.

After a run starts, the `Results` page shows live progress and polls `/run/status`. You can request a cooperative stop with `Stop Run`. Completed and in-progress results include success rate, attempts/sec, timeout and failure counts, duration percentiles, per-stage Web Messaging percentiles, live diagnostics, completed attempt details, adaptive adjustments, schedule status, and local run history.

## Scheduling

The `Run` page can save the current run configuration as one persistent automatic schedule. Manual runs remain separate from scheduled runs.

Supported schedule cadences:

- `hourly`: runs at the selected minute of each hour.
- `daily`: runs at the selected local time.
- `weekly`: runs on the selected weekday and local time.
- `monthly`: runs on the selected day of month and local time. Months with fewer days use the last valid day of that month.

Schedules use an IANA timezone such as `UTC` or `America/New_York`. The start date defaults to the current local date when blank, and the end date is required. Schedule state is stored locally in `model_warmup_schedule.json` under the history directory.

## Local Data

By default, local data is written to `.ava_warmup_history/`.

- `index.json`: local run history index.
- `runs/`: full JSON reports for recent runs.
- `model_warmup_schedule.json`: persistent schedule state.

History retention defaults to `50` runs. The newest `20` reports stay as full JSON, the next `20` are compressed as gzip JSON, and older retained entries become summary-only. Do not commit `.ava_warmup_history/`, `.env`, `config.yaml`, deployment IDs, raw transcripts, or customer conversation artifacts.

## Configuration

The form can supply deployment and region at run time. Environment variables can set defaults and tune runtime behavior:

- `AVA_WARMUP_DEPLOYMENT_ID`: default Web Messaging deployment ID. Compatibility alias: `GC_DEPLOYMENT_ID`.
- `AVA_WARMUP_REGION`: default Genesys Cloud region, for example `mypurecloud.com` or `usw2.pure.cloud`. Compatibility alias: `GC_REGION`.
- `AVA_WARMUP_RESPONSE_TIMEOUT`: per-stage Web Messaging timeout in seconds. Default: `90`.
- `AVA_WARMUP_SUCCESS_THRESHOLD`: regression threshold for completion rate, clamped to `0.0..1.0`. Default: `0.8`.
- `AVA_WARMUP_PERFORMANCE_DIAGNOSTICS_ENABLED`: include compact performance diagnostics. Default: `true`.
- `AVA_WARMUP_DEBUG_CAPTURE_FRAMES`: capture debug WebSocket frames on attempts. Default: `false`.
- `AVA_WARMUP_DEBUG_FRAME_LIMIT`: maximum debug frames per attempt. Default: `8`.
- `AVA_WARMUP_HISTORY_DIR`: local run and schedule history directory. Default: `.ava_warmup_history`.
- `GC_TESTER_HISTORY_DIR`: compatibility fallback for the history directory when `AVA_WARMUP_HISTORY_DIR` is unset.
- `AVA_WARMUP_HISTORY_MAX_RUNS`: maximum retained history entries. Default: `50`.
- `AVA_WARMUP_HISTORY_FULL_JSON_RUNS`: newest reports kept as full JSON. Default: `20`.
- `AVA_WARMUP_HISTORY_GZIP_RUNS`: additional reports kept as gzip JSON before summary-only compaction. Default: `20`.

Example:

```bash
export AVA_WARMUP_DEPLOYMENT_ID="your-deployment-id"
export AVA_WARMUP_REGION="mypurecloud.com"
export AVA_WARMUP_HISTORY_DIR=".ava_warmup_history"
python3 -m ava_warmup
```

## HTTP API

All run and schedule endpoints accept form data. Endpoints that receive JSON, or requests with `Accept: application/json`, return JSON responses.

- `GET /`: render the run and schedule page.
- `POST /run/model_warm_up`: start a background run. Accepts `deployment_id`, `region`, optional `recorded_model`, `attempt_count`, `execution_mode`, `worker_count`, `pacing_seconds`, and `performance_profile`. JSON success returns `202` with `{ok, run_id, results_url}`. Validation errors return `400`; an active run returns `409`.
- `GET /run/status`: return active run state, trigger source, stop state, warm-up metadata, live progress, and recent progress events.
- `POST /run/stop`: request that the active run stop. JSON success returns `{ok, stop_requested}`; no active run returns `409`.
- `POST /run/model_warm_up/schedule`: save and enable the persistent schedule. Accepts the run fields plus schedule fields such as `cadence`, `timezone_name`, `start_date`, `end_date`, `minute`, `time_hhmm`, `weekday`, and `day_of_month`.
- `POST /run/model_warm_up/schedule/cancel`: cancel the schedule.
- `POST /run/model_warm_up/schedule/disable`: cancel the schedule; equivalent to `/cancel`.
- `GET /run/model_warm_up/schedule/status`: return persisted schedule status.
- `GET /results`: render the metrics dashboard. Use `?history_run_id=<run_id>` to view a specific retained history run.
- `GET /results/history?limit=100`: return local warm-up run history. `limit` is clamped to `1..100`.
- `GET /results/export?format=json`: export the latest, in-progress, or selected report as JSON.
- `GET /results/export?format=csv`: export a compact metrics CSV named `ava_spec_warm_up_metrics.csv`.
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
