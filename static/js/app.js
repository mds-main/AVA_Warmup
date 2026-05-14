/* ============================================================
   AVA Mission Control — SPA front-end
   ============================================================
   Vanilla JS app that reads server-rendered bootstrap data,
   polls /run/status while a warm-up is active, and reflects
   real backend state in the cockpit, attempts grid, latency
   chart, diagnostics stream, history, and schedule strip.
   ============================================================ */
(function () {
  'use strict';

  // ----------------------------------------------------------------
  // Bootstrap data from server
  // ----------------------------------------------------------------
  function readBootstrap() {
    var node = document.getElementById('ava-bootstrap');
    if (!node) return {};
    try {
      return JSON.parse(node.textContent || '{}');
    } catch (err) {
      console.error('Failed to parse bootstrap JSON', err);
      return {};
    }
  }

  var bootstrap = readBootstrap();

  // ----------------------------------------------------------------
  // App state
  // ----------------------------------------------------------------
  var state = {
    runActive: !!bootstrap.run_active,
    stopRequested: !!bootstrap.stop_requested,
    activeRunId: bootstrap.active_run_id || null,
    triggerSource: bootstrap.trigger_source || 'manual',
    warmup: bootstrap.warmup || null,
    liveProgress: bootstrap.live_progress || null,
    report: bootstrap.report || null,
    progressEvents: bootstrap.progress_events || [],
    history: bootstrap.history || [],
    schedules: (bootstrap.schedule_status && bootstrap.schedule_status.scheduled_warmups) || [],
    scheduleStatus: bootstrap.schedule_status || {},
    suites: bootstrap.suites || [],
    selectedSuiteId: bootstrap.selected_suite_id || (bootstrap.suites && bootstrap.suites[0] && bootstrap.suites[0].suite_id) || 'ava_spec_default',
    regions: bootstrap.regions || ['mypurecloud.com'],
    config: bootstrap.config || {},
    pacingChoices: bootstrap.pacing_choices || [0.5, 1.0, 2.5, 5.0, 7.5],
    defaultAttempts: bootstrap.default_attempts || 228,
    viewingHistoryRunId: bootstrap.viewing_history_run_id || null,

    // derived/live
    feedFilter: 'all',
    feedPaused: false,
    drawerOpen: false,
    drawerTab: 'target',
    detailAttempt: null,
    dashboardReloadPaused: (function () {
      try { return localStorage.getItem('ava_warmup.auto_refresh_paused') === '1'; }
      catch (e) { return false; }
    })(),

    // chart series — populated from real attempt data
    latencySeries: [],   // [{t, p50, p95, p99}]
    throughputSeries: [], // numeric, attempts/sec rolling
    attempts: [],         // [{n, status, durationS, message, attempt_number}]
    attemptStats: { ok: 0, warn: 0, err: 0, running: 0, pending: 0, skipped: 0 },

    // per-stage rolling timings (from attempt.warmup_stage_durations_ms)
    stageSamples: {},  // stage -> array of ms

    // config form working copy
    cfg: null,
  };

  // Cadence helpers — env may set default_cadence to a named value
  // (hourly/daily/weekly/monthly) or a positive-integer "every N hours" string.
  function parseCadenceHours(value) {
    var n = parseInt(value, 10);
    if (isNaN(n) || n < 1) return 0;
    return n;
  }
  function normalizeCadenceFromConfig(value) {
    var raw = String(value || '').trim().toLowerCase();
    if (['hourly', 'daily', 'weekly', 'monthly'].indexOf(raw) !== -1) return raw;
    if (parseCadenceHours(raw) > 0) return 'interval';
    return 'daily';
  }

  // Pre-fill cfg from server defaults (env-driven AppConfig).
  state.cfg = {
    deployment_id: state.config.gc_deployment_id || '',
    region: state.config.gc_region || (state.regions[0] || 'mypurecloud.com'),
    recorded_model: 'gemma4:e4b',
    attempt_count: state.config.default_attempt_count || state.defaultAttempts,
    execution_mode: state.config.default_execution_mode || 'serial',
    worker_count: state.config.default_worker_count || 1,
    pacing_seconds: Number(state.config.default_pacing_seconds) || 1.0,
    performance_profile: state.config.default_performance_profile || 'safe_adaptive',
    suite_id: state.selectedSuiteId,
    cadence: normalizeCadenceFromConfig(state.config.default_cadence),
    cadence_hours: parseCadenceHours(state.config.default_cadence) || 3,
    timezone_name: state.config.default_timezone || 'UTC',
    time_hhmm: state.config.default_time_hhmm || '02:00',
    minute: state.config.default_minute != null ? state.config.default_minute : 0,
    weekday: state.config.default_weekday != null ? state.config.default_weekday : 0,
    day_of_month: state.config.default_day_of_month != null ? state.config.default_day_of_month : 1,
    start_date: state.config.default_schedule_start_date || '',
    end_date: state.config.default_schedule_end_date || '',
  };

  // ----------------------------------------------------------------
  // Helpers
  // ----------------------------------------------------------------
  function $(id) { return document.getElementById(id); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return '—';
    seconds = Math.round(Number(seconds));
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (m === 0) return s + 's';
    if (m < 60) return m + 'm ' + String(s).padStart(2, '0') + 's';
    var h = Math.floor(m / 60);
    return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function formatTimeOfDay(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      var hh = String(d.getHours()).padStart(2, '0');
      var mm = String(d.getMinutes()).padStart(2, '0');
      var ss = String(d.getSeconds()).padStart(2, '0');
      var ms = String(d.getMilliseconds()).padStart(3, '0');
      return hh + ':' + mm + ':' + ss + '.' + ms;
    } catch (e) {
      return String(iso);
    }
  }

  function percentile(sortedAsc, rank) {
    if (!sortedAsc.length) return 0;
    if (sortedAsc.length === 1) return sortedAsc[0];
    var pos = (sortedAsc.length - 1) * rank;
    var lo = Math.floor(pos);
    var hi = Math.min(lo + 1, sortedAsc.length - 1);
    var w = pos - lo;
    return sortedAsc[lo] + (sortedAsc[hi] - sortedAsc[lo]) * w;
  }

  // ----------------------------------------------------------------
  // Number/axis helpers
  // ----------------------------------------------------------------
  // "Nice number" algorithm (Heckbert, Graphics Gems) — picks tick spacings
  // that read as clean human numbers (1, 2, 5, 10 × 10^n), so the y-axis
  // labels are always round values like 0/500/1000/1500ms instead of
  // arbitrary fractions of the data max.
  function niceNum(range, round) {
    if (range <= 0) return 1;
    var exp = Math.floor(Math.log10(range));
    var frac = range / Math.pow(10, exp);
    var niceFrac;
    if (round) {
      if (frac < 1.5) niceFrac = 1;
      else if (frac < 3) niceFrac = 2;
      else if (frac < 7) niceFrac = 5;
      else niceFrac = 10;
    } else {
      if (frac <= 1) niceFrac = 1;
      else if (frac <= 2) niceFrac = 2;
      else if (frac <= 5) niceFrac = 5;
      else niceFrac = 10;
    }
    return niceFrac * Math.pow(10, exp);
  }

  function niceScale(min, max, targetTicks) {
    targetTicks = targetTicks || 5;
    if (!isFinite(min)) min = 0;
    if (!isFinite(max) || max <= min) max = min + 1;
    var range = niceNum(max - min, false);
    var step = niceNum(range / Math.max(1, targetTicks - 1), true);
    var niceMin = Math.floor(min / step) * step;
    var niceMax = Math.ceil(max / step) * step;
    var ticks = [];
    var epsilon = step * 1e-9;
    for (var v = niceMin; v <= niceMax + epsilon; v += step) {
      // Round to avoid floating-point cruft like 0.30000000000000004.
      ticks.push(Math.round(v / step) * step);
    }
    return { min: niceMin, max: niceMax, step: step, ticks: ticks };
  }

  function formatMs(ms) {
    if (!isFinite(ms) || ms < 0) return '—';
    if (ms < 1) return ms.toFixed(2) + ' ms';
    if (ms < 10) return ms.toFixed(1) + ' ms';
    if (ms < 10000) return Math.round(ms) + ' ms';
    return (ms / 1000).toFixed(2) + ' s';
  }

  function formatAxisMs(ms) {
    if (!isFinite(ms)) return '';
    if (ms === 0) return '0';
    if (Math.abs(ms) < 1000) return Math.round(ms) + ' ms';
    var seconds = ms / 1000;
    if (Math.abs(seconds) < 10) return seconds.toFixed(1) + ' s';
    return Math.round(seconds) + ' s';
  }

  function formatAttemptsPerSec(v) {
    if (!isFinite(v) || v < 0) return '—';
    if (v >= 100) return v.toFixed(0) + '/s';
    if (v >= 10) return v.toFixed(1) + '/s';
    if (v >= 1) return v.toFixed(2) + '/s';
    return v.toFixed(3) + '/s';
  }

  function formatAxisRate(v) {
    if (!isFinite(v)) return '';
    if (v === 0) return '0';
    if (v >= 10) return v.toFixed(0) + '/s';
    if (v >= 1) return v.toFixed(1) + '/s';
    return v.toFixed(2) + '/s';
  }

  // Track per-chart layout so resize observers can re-render with new pixel
  // dimensions. preserveAspectRatio="none" used to stretch SVG text and
  // strokes; we now size each chart's viewBox to its actual rendered pixel
  // box and re-render whenever the container size changes.
  var chartObservers = {};
  function observeChartResize(containerId, redraw) {
    if (chartObservers[containerId]) return;
    var container = document.getElementById(containerId);
    if (!container || typeof ResizeObserver === 'undefined') return;
    var observer = new ResizeObserver(function () { redraw(); });
    observer.observe(container);
    chartObservers[containerId] = observer;
  }

  function deploymentLabel(id) {
    if (!id) return 'unconfigured';
    if (id.length <= 14) return id;
    return id.slice(0, 8) + '…' + id.slice(-4);
  }

  function attemptStatusFromResult(res) {
    if (!res) return 'pending';
    if (res.skipped) return 'skipped';
    if (res.success) return 'ok';
    if (res.timed_out) return 'warn';
    return 'err';
  }

  // ----------------------------------------------------------------
  // Status pulse helper
  // ----------------------------------------------------------------
  function setPulse(el, state) {
    if (!el) return;
    var cls = 'pulse pulse--idle';
    var label = 'Idle';
    if (state === 'running') { cls = 'pulse pulse--live'; label = 'Live'; }
    else if (state === 'paused') { cls = 'pulse pulse--warn'; label = 'Paused'; }
    else if (state === 'done') { cls = 'pulse pulse--ok'; label = 'Complete'; }
    else if (state === 'error') { cls = 'pulse pulse--err'; label = 'Error'; }
    else if (state === 'stopping') { cls = 'pulse pulse--warn'; label = 'Stopping'; }
    el.className = cls;
    el.innerHTML = '<span class="dot"></span> ' + label;
  }

  function currentRunPhase() {
    if (state.runActive) {
      if (state.stopRequested) return 'stopping';
      return 'running';
    }
    if (state.report && state.report.stopped_by_user) return 'done';
    if (state.report) return 'done';
    return 'idle';
  }

  // ----------------------------------------------------------------
  // Top bar updates
  // ----------------------------------------------------------------
  function renderTopBar() {
    var phase = currentRunPhase();
    setPulse($('topbar-run-pulse'), phase);
    setPulse($('cockpit-pulse'), phase);

    var actions = $('topbar-actions');
    if (actions) {
      var html = '';
      if (phase === 'idle' || phase === 'done') {
        html = '<button class="btn btn--primary btn--collapse-label" data-action="start" title="Start warm-up">'
             + '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8Z"/></svg>'
             + 'Start Warm Up <span class="kbd">⌘↵</span></button>';
      } else if (phase === 'running' || phase === 'stopping') {
        var paused = !!state.dashboardReloadPaused;
        var refreshIcon = paused
          ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
          : '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
        var refreshLabel = paused ? 'Resume auto-refresh' : 'Pause auto-refresh';
        html += '<button class="btn btn--collapse-label" data-action="toggle-auto-refresh" title="' + refreshLabel + '" aria-pressed="' + (paused ? 'true' : 'false') + '">'
             + refreshIcon + refreshLabel + '</button>';
        html += '<button class="btn btn--danger btn--collapse-label" data-action="stop" title="Stop"' + (phase === 'stopping' ? ' disabled' : '') + '>'
             + '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h12v12H6z"/></svg>'
             + (phase === 'stopping' ? 'Stopping…' : 'Stop Run') + '</button>';
      }
      actions.innerHTML = html;
    }

    // Deployment chip
    var depBar = $('topbar-deployment');
    if (depBar) {
      var depId = state.warmup && state.warmup.deployment_id ? state.warmup.deployment_id : (state.cfg.deployment_id || state.config.gc_deployment_id || '');
      var region = state.warmup && state.warmup.region ? state.warmup.region : (state.cfg.region || state.config.gc_region || 'mypurecloud.com');
      var dotIdle = !depId ? ' idle' : '';
      depBar.innerHTML = '<span class="dot' + dotIdle + '"></span>'
        + '<span class="region">' + escapeHtml(region) + '</span>'
        + '<span class="pipe">/</span>'
        + '<span class="id">' + escapeHtml(deploymentLabel(depId)) + '</span>';
    }

    // Model chip
    var mc = $('topbar-model');
    if (mc) mc.textContent = (state.warmup && state.warmup.recorded_model) || state.cfg.recorded_model || 'gemma4:e4b';
  }

  // ----------------------------------------------------------------
  // Cockpit
  // ----------------------------------------------------------------
  function renderCockpit() {
    var planned = (state.warmup && state.warmup.planned_attempts) || state.liveProgress && state.liveProgress.planned_attempts || (state.report && state.report.overall_attempts) || 0;
    var completed = (state.liveProgress && state.liveProgress.completed_attempts) || (state.warmup && state.warmup.completed_attempts) || (state.report && state.report.overall_attempts) || 0;
    var pct = planned ? Math.min(1, completed / planned) : 0;

    // Ring
    var ring = $('ring-progress');
    if (ring) {
      var C = 2 * Math.PI * 78;
      ring.setAttribute('stroke-dasharray', C.toFixed(2));
      ring.setAttribute('stroke-dashoffset', (C * (1 - pct)).toFixed(2));
    }
    var pctEl = $('ring-pct'); if (pctEl) pctEl.textContent = (pct * 100).toFixed(0) + '%';
    var countsEl = $('ring-counts');
    if (countsEl) countsEl.textContent = completed.toLocaleString() + ' / ' + planned.toLocaleString();

    // Titles
    var ttl = $('cockpit-suite');
    var sce = $('cockpit-scenario');
    var suite = state.warmup || state.report || {};
    if (ttl) ttl.textContent = suite.suite_name || (state.report && state.report.suite_name) || pickSuite().suite_name || 'AVA Spec Warm Up Suite';
    if (sce) sce.textContent = suite.scenario_name || pickSuite().scenario_name || 'No Help Needed Warm Up';

    // Metrics
    var successes, total, rate;
    if (state.report) {
      successes = state.report.overall_successes || 0;
      total = state.report.overall_attempts || 1;
      rate = state.report.overall_success_rate || (successes / total);
    } else if (state.runActive) {
      // approximate from completed attempts so far
      var ok = state.attempts.filter(function (a) { return a.status === 'ok'; }).length;
      var doneCount = state.attempts.filter(function (a) { return a.status === 'ok' || a.status === 'warn' || a.status === 'err'; }).length;
      successes = ok;
      total = doneCount || 1;
      rate = doneCount > 0 ? ok / doneCount : 0;
    } else {
      successes = 0; total = 0; rate = 0;
    }
    setMetric('m-success-rate', total > 0 ? (rate * 100).toFixed(1) + '%' : '—', rate > 0.95 ? 'ok' : rate > 0.85 ? 'warn' : (total > 0 ? 'err' : ''));

    var aps = (state.liveProgress && state.liveProgress.attempts_per_second) || (state.warmup && state.warmup.attempts_per_second) || 0;
    setMetric('m-aps', aps ? aps.toFixed(3) + '/s' : '—');

    var elapsed = (state.liveProgress && state.liveProgress.elapsed_seconds) || (state.report && state.report.duration_seconds) || 0;
    setMetric('m-elapsed', elapsed ? formatDuration(elapsed) : '—');

    var etaSecs = state.liveProgress && state.liveProgress.estimated_remaining_seconds;
    setMetric('m-eta', state.runActive && etaSecs != null ? formatDuration(etaSecs) : (state.report ? 'done' : '—'));

    var pacing = (state.warmup && state.warmup.effective_pacing_seconds) || state.cfg.pacing_seconds;
    setMetric('m-pacing', pacing.toFixed(1) + 's');

    var workers = (state.warmup && state.warmup.effective_worker_count) || state.cfg.worker_count;
    var mode = (state.warmup && state.warmup.execution_mode) || state.cfg.execution_mode;
    setMetric('m-workers', workers + ' ' + (mode === 'parallel' ? 'parallel' : 'serial'));

    renderThroughputSpark();
    renderCockpitActions();
  }

  function setMetric(id, value, kind) {
    var el = $(id);
    if (!el) return;
    el.textContent = value;
    el.className = 'v ' + (kind || '');
  }

  function renderCockpitActions() {
    var box = $('cockpit-actions');
    if (!box) return;
    var phase = currentRunPhase();
    var html = '';
    if (phase === 'idle' || phase === 'done') {
      html += '<button class="btn btn--primary btn--lg" data-action="start">'
            + '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8Z"/></svg>'
            + 'Start Warm Up</button>';
    } else if (phase === 'running' || phase === 'stopping') {
      html += '<button class="btn btn--lg btn--danger" data-action="stop"' + (phase === 'stopping' ? ' disabled' : '') + '>'
            + '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h12v12H6z"/></svg>'
            + (phase === 'stopping' ? 'Stopping…' : 'Stop Run') + '</button>';
    }
    html += '<button class="btn" data-action="configure">'
          + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/></svg>'
          + 'Configure run</button>';
    html += '<a class="btn btn--ghost btn--sm capture-hidden" href="/results/export?format=json' + (state.viewingHistoryRunId ? '&history_run_id=' + encodeURIComponent(state.viewingHistoryRunId) : '') + '">'
          + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/></svg>'
          + 'Export results</a>';
    box.innerHTML = html;
  }

  function pickSuite() {
    var preferred = (state.cfg && state.cfg.suite_id) || state.selectedSuiteId;
    return (state.suites || []).find(function (s) { return s.suite_id === preferred; }) || (state.suites && state.suites[0]) || { suite_name: 'AVA Spec Warm Up Suite', scenario_name: 'No Help Needed Warm Up', messages: ['no help needed'] };
  }

  // ----------------------------------------------------------------
  // Throughput sparkline
  //   Small in-cockpit chart showing recent attempts/sec trend. The viewBox
  //   is sized to actual rendered pixels each draw so the area gradient and
  //   line strokes stay 1:1 and the hover tooltip can map mouse->index
  //   without distortion.
  // ----------------------------------------------------------------
  function renderThroughputSpark() {
    var container = $('throughput-spark');
    if (!container) return;
    var svg = container.querySelector('svg');
    var emptyEl = container.querySelector('.spark__empty');
    var data = state.throughputSeries.slice();
    if (!data.length || !svg) {
      if (svg) svg.innerHTML = '';
      if (emptyEl) emptyEl.style.display = '';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';

    var rect = container.getBoundingClientRect();
    var w = Math.max(120, Math.round(rect.width));
    var h = Math.max(36, Math.round(rect.height));
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);

    var maxV = Math.max.apply(null, data);
    var minV = 0;
    if (!isFinite(maxV) || maxV <= 0) maxV = 1;
    var pad = { l: 0, r: 0, t: 4, b: 4 };
    function ys(v) {
      return h - pad.b - ((v - minV) / (maxV - minV || 1)) * (h - pad.t - pad.b);
    }
    function xs(i) {
      if (data.length < 2) return w / 2;
      return pad.l + (i / (data.length - 1)) * (w - pad.l - pad.r);
    }

    var pts = data.map(function (v, i) { return [xs(i), ys(v)]; });
    var lineD = pts.map(function (p, i) {
      return (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    }).join(' ');
    var areaD = lineD
      + ' L ' + xs(data.length - 1).toFixed(1) + ',' + (h - pad.b).toFixed(1)
      + ' L ' + xs(0).toFixed(1) + ',' + (h - pad.b).toFixed(1)
      + ' Z';

    var latest = data[data.length - 1];

    svg.innerHTML =
      '<defs>'
      + '<linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0" stop-color="oklch(78% 0.16 65 / 0.45)"/>'
      + '<stop offset="1" stop-color="oklch(78% 0.16 65 / 0)"/>'
      + '</linearGradient>'
      + '</defs>'
      + '<line class="baseline" x1="0" x2="' + w + '" y1="' + (h - pad.b) + '" y2="' + (h - pad.b) + '"/>'
      + '<path class="area" d="' + areaD + '"/>'
      + '<path class="line" d="' + lineD + '"/>'
      + '<circle class="spark__current" cx="' + xs(data.length - 1).toFixed(1) + '" cy="' + ys(latest).toFixed(1) + '" r="2.5"/>';

    // Cache geometry for hover lookups.
    container._spark = { data: data, xs: xs, ys: ys, pad: pad, w: w, h: h };

    observeChartResize('throughput-spark', renderThroughputSpark);
  }

  function attachSparkHover() {
    var container = $('throughput-spark');
    if (!container || container._hoverAttached) return;
    container._hoverAttached = true;
    var tooltip = ensureChartTooltip();
    container.addEventListener('mousemove', function (e) {
      var geom = container._spark;
      if (!geom || !geom.data.length) { hideChartTooltip(); return; }
      var rect = container.getBoundingClientRect();
      var x = e.clientX - rect.left;
      // Map pixel x → index within data.
      var idx = Math.round((x / Math.max(1, rect.width)) * (geom.data.length - 1));
      idx = Math.max(0, Math.min(geom.data.length - 1, idx));
      var value = geom.data[idx];
      showChartTooltip(tooltip, e.clientX, e.clientY, [
        { label: 'attempts/sec', value: formatAttemptsPerSec(value), color: 'var(--amber)' },
        { label: 'sample', value: '#' + (idx + 1) + ' of ' + geom.data.length, muted: true },
      ]);
    });
    container.addEventListener('mouseleave', hideChartTooltip);
  }

  // ----------------------------------------------------------------
  // Latency chart
  //   Live rolling p50/p95/p99 attempt latency. The chart sizes its viewBox
  //   to actual rendered pixels (via ResizeObserver) so axis text and stroke
  //   widths render at 1:1 instead of stretching. Y-ticks come from a
  //   nice-number algorithm so labels are always clean (0/500/1000/1500ms
  //   etc) rather than arbitrary fractions of the data max.
  // ----------------------------------------------------------------
  function renderLatencyChart() {
    var container = $('lat-chart');
    var svg = $('lat-chart-svg');
    var empty = $('lat-chart-empty');
    var rightLabels = $('lat-chart-current');
    if (!container || !svg) return;
    var data = state.latencySeries.slice();
    if (!data.length) {
      svg.innerHTML = '';
      if (empty) empty.style.display = '';
      if (rightLabels) rightLabels.innerHTML = '';
      container._lat = null;
      observeChartResize('lat-chart', renderLatencyChart);
      return;
    }
    if (empty) empty.style.display = 'none';

    var rect = container.getBoundingClientRect();
    var w = Math.max(280, Math.round(rect.width));
    var h = Math.max(180, Math.round(rect.height));
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);

    var pad = { l: 56, r: 70, t: 14, b: 26 };
    var plotW = w - pad.l - pad.r;
    var plotH = h - pad.t - pad.b;

    var maxV = Math.max.apply(null, data.map(function (d) { return d.p99; }));
    if (!isFinite(maxV) || maxV <= 0) maxV = 1;
    var scale = niceScale(0, maxV, 5);

    function xs(i) {
      if (data.length < 2) return pad.l + plotW / 2;
      return pad.l + (i / (data.length - 1)) * plotW;
    }
    function ys(v) {
      return pad.t + plotH - ((v - scale.min) / (scale.max - scale.min || 1)) * plotH;
    }

    function pathFor(key) {
      return data.map(function (d, i) {
        return (i === 0 ? 'M' : 'L') + xs(i).toFixed(1) + ',' + ys(d[key]).toFixed(1);
      }).join(' ');
    }
    function bandFor(a, b) {
      var top = data.map(function (d, i) {
        return (i === 0 ? 'M' : 'L') + xs(i).toFixed(1) + ',' + ys(d[a]).toFixed(1);
      }).join(' ');
      var bottom = '';
      for (var i = data.length - 1; i >= 0; i--) {
        bottom += 'L' + xs(i).toFixed(1) + ',' + ys(data[i][b]).toFixed(1) + ' ';
      }
      return top + ' ' + bottom + 'Z';
    }

    // Grid lines + y-axis labels with nice numbers.
    var grid = '';
    var yaxis = '';
    scale.ticks.forEach(function (tick) {
      var y = ys(tick);
      grid += '<line x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1) + '"/>';
      yaxis += '<text x="' + (pad.l - 8) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + escapeHtml(formatAxisMs(tick)) + '</text>';
    });

    // X-axis: show attempt-window markers at 0%, 25%, 50%, 75%, 100% of the
    // window. With our rolling window of `latencySeries`, each entry maps to
    // an attempt index t (1-based). Use that for honest labels.
    var xaxis = '';
    var xMarks = [0, 0.25, 0.5, 0.75, 1];
    xMarks.forEach(function (frac) {
      var i = Math.round(frac * (data.length - 1));
      var attemptNumber = data[i] && data[i].t ? data[i].t : (i + 1);
      var x = xs(i);
      xaxis += '<line x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' + (pad.t + plotH) + '" y2="' + (pad.t + plotH + 4) + '"/>';
      xaxis += '<text x="' + x.toFixed(1) + '" y="' + (h - pad.b + 16) + '" text-anchor="middle">#' + attemptNumber + '</text>';
    });

    // Axis frame.
    var frame =
      '<line class="axis-line" x1="' + pad.l + '" y1="' + pad.t + '" x2="' + pad.l + '" y2="' + (pad.t + plotH) + '"/>'
      + '<line class="axis-line" x1="' + pad.l + '" y1="' + (pad.t + plotH) + '" x2="' + (w - pad.r) + '" y2="' + (pad.t + plotH) + '"/>';

    var bandSvg =
      '<path class="band-p99" d="' + bandFor('p95', 'p99') + '"/>'
      + '<path class="band-p50" d="' + bandFor('p50', 'p95') + '"/>';
    var lineSvg =
      '<path class="line-p99" d="' + pathFor('p99') + '"/>'
      + '<path class="line-p95" d="' + pathFor('p95') + '"/>'
      + '<path class="line-p50" d="' + pathFor('p50') + '"/>';

    // Crosshair (hidden until hover sets coordinates).
    var crosshair =
      '<line id="lat-crosshair-v" class="crosshair" x1="0" x2="0" y1="' + pad.t + '" y2="' + (pad.t + plotH) + '" style="display:none"/>'
      + '<circle id="lat-crosshair-p50" class="crosshair-dot dot-p50" cx="0" cy="0" r="3.5" style="display:none"/>'
      + '<circle id="lat-crosshair-p95" class="crosshair-dot dot-p95" cx="0" cy="0" r="3.5" style="display:none"/>'
      + '<circle id="lat-crosshair-p99" class="crosshair-dot dot-p99" cx="0" cy="0" r="3.5" style="display:none"/>';

    svg.innerHTML =
      '<g class="grid">' + grid + '</g>'
      + frame
      + bandSvg
      + lineSvg
      + '<g class="axis y-axis">' + yaxis + '</g>'
      + '<g class="axis x-axis">' + xaxis + '</g>'
      + crosshair;

    // Right-edge "current value" annotations so users can read the latest
    // p50/p95/p99 without hovering.
    if (rightLabels) {
      var latest = data[data.length - 1];
      rightLabels.innerHTML =
        '<div class="lat-chart__pill p50" title="latest p50">'
        + '<span class="sw"></span><span class="lbl">p50</span><span class="val">' + escapeHtml(formatMs(latest.p50)) + '</span>'
        + '</div>'
        + '<div class="lat-chart__pill p95" title="latest p95">'
        + '<span class="sw"></span><span class="lbl">p95</span><span class="val">' + escapeHtml(formatMs(latest.p95)) + '</span>'
        + '</div>'
        + '<div class="lat-chart__pill p99" title="latest p99">'
        + '<span class="sw"></span><span class="lbl">p99</span><span class="val">' + escapeHtml(formatMs(latest.p99)) + '</span>'
        + '</div>';
    }

    container._lat = { data: data, xs: xs, ys: ys, pad: pad, w: w, h: h, plotW: plotW, scale: scale };
    observeChartResize('lat-chart', renderLatencyChart);
  }

  function attachLatencyHover() {
    var container = $('lat-chart');
    if (!container || container._hoverAttached) return;
    container._hoverAttached = true;
    var tooltip = ensureChartTooltip();
    var svg = $('lat-chart-svg');

    function moveCrosshair(idx) {
      var geom = container._lat;
      if (!geom) return;
      var d = geom.data[idx];
      var cx = geom.xs(idx);
      var v = svg.querySelector('#lat-crosshair-v');
      var p50 = svg.querySelector('#lat-crosshair-p50');
      var p95 = svg.querySelector('#lat-crosshair-p95');
      var p99 = svg.querySelector('#lat-crosshair-p99');
      if (v) { v.setAttribute('x1', cx.toFixed(1)); v.setAttribute('x2', cx.toFixed(1)); v.style.display = ''; }
      if (p50) { p50.setAttribute('cx', cx.toFixed(1)); p50.setAttribute('cy', geom.ys(d.p50).toFixed(1)); p50.style.display = ''; }
      if (p95) { p95.setAttribute('cx', cx.toFixed(1)); p95.setAttribute('cy', geom.ys(d.p95).toFixed(1)); p95.style.display = ''; }
      if (p99) { p99.setAttribute('cx', cx.toFixed(1)); p99.setAttribute('cy', geom.ys(d.p99).toFixed(1)); p99.style.display = ''; }
    }
    function hideCrosshair() {
      ['#lat-crosshair-v', '#lat-crosshair-p50', '#lat-crosshair-p95', '#lat-crosshair-p99'].forEach(function (sel) {
        var el = svg.querySelector(sel);
        if (el) el.style.display = 'none';
      });
    }

    container.addEventListener('mousemove', function (e) {
      var geom = container._lat;
      if (!geom || !geom.data.length) { hideChartTooltip(); hideCrosshair(); return; }
      var rect = container.getBoundingClientRect();
      var scaleX = geom.w / rect.width;
      var xInView = (e.clientX - rect.left) * scaleX;
      if (xInView < geom.pad.l || xInView > geom.w - 0) {
        hideChartTooltip();
        hideCrosshair();
        return;
      }
      // Find nearest data index.
      var idx = 0;
      var bestDist = Infinity;
      for (var i = 0; i < geom.data.length; i++) {
        var d = Math.abs(geom.xs(i) - xInView);
        if (d < bestDist) { bestDist = d; idx = i; }
      }
      var sample = geom.data[idx];
      moveCrosshair(idx);
      var attemptNumber = sample.t || (idx + 1);
      showChartTooltip(tooltip, e.clientX, e.clientY, [
        { label: 'attempt window', value: '#' + attemptNumber, muted: true },
        { label: 'p50', value: formatMs(sample.p50), color: 'var(--ok)' },
        { label: 'p95', value: formatMs(sample.p95), color: 'var(--warn)' },
        { label: 'p99', value: formatMs(sample.p99), color: 'var(--err)' },
      ]);
    });
    container.addEventListener('mouseleave', function () {
      hideChartTooltip();
      hideCrosshair();
    });
  }

  // ----------------------------------------------------------------
  // Shared chart tooltip
  // ----------------------------------------------------------------
  function ensureChartTooltip() {
    var tip = document.getElementById('chart-tooltip');
    if (tip) return tip;
    tip = document.createElement('div');
    tip.id = 'chart-tooltip';
    tip.className = 'chart-tooltip';
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }
  function showChartTooltip(tip, clientX, clientY, rows) {
    var html = rows.map(function (r) {
      var swStyle = r.color ? 'background:' + r.color : 'background:transparent;border:1px dashed var(--line)';
      var rowClass = 'chart-tooltip__row' + (r.muted ? ' muted' : '');
      return '<div class="' + rowClass + '">'
        + '<span class="chart-tooltip__sw" style="' + swStyle + '"></span>'
        + '<span class="chart-tooltip__lbl">' + escapeHtml(r.label) + '</span>'
        + '<span class="chart-tooltip__val">' + escapeHtml(r.value) + '</span>'
        + '</div>';
    }).join('');
    tip.innerHTML = html;
    tip.hidden = false;
    // Position with a small offset; flip to the left/up edge when near screen bounds.
    var rect = tip.getBoundingClientRect();
    var offset = 14;
    var x = clientX + offset;
    var y = clientY + offset;
    if (x + rect.width + 8 > window.innerWidth) x = clientX - rect.width - offset;
    if (y + rect.height + 8 > window.innerHeight) y = clientY - rect.height - offset;
    if (x < 4) x = 4;
    if (y < 4) y = 4;
    tip.style.transform = 'translate(' + Math.round(x) + 'px,' + Math.round(y) + 'px)';
  }
  function hideChartTooltip() {
    var tip = document.getElementById('chart-tooltip');
    if (tip) tip.hidden = true;
  }

  // ----------------------------------------------------------------
  // Stage timings
  //   Horizontal bar per stage showing p50 / p95 / p99 against the slowest
  //   stage's p99 as the shared max. Bars layer p99 (back) → p95 (mid) →
  //   p50 (front) so the three percentiles are simultaneously legible.
  //   Hovering a row reveals a precise tooltip with values + share of total.
  // ----------------------------------------------------------------
  function renderStageTimings() {
    var body = $('stage-timings-body');
    if (!body) return;
    var stages = stageSummary();
    if (!stages.length) {
      body.innerHTML = '<div class="stage__empty">No stage timings yet — start a run to populate.</div>';
      return;
    }
    var max = Math.max.apply(null, stages.map(function (s) { return s.p99; }));
    if (!isFinite(max) || max <= 0) max = 1;
    var totalP50 = stages.reduce(function (acc, s) { return acc + (s.p50 || 0); }, 0) || 1;

    body.innerHTML = stages.map(function (s, idx) {
      var shareOfP50 = ((s.p50 / totalP50) * 100);
      var dataStage = encodeURIComponent(JSON.stringify({
        name: s.name,
        p50: s.p50,
        p95: s.p95,
        p99: s.p99,
        share: shareOfP50,
      }));
      return '<div class="stage" data-stage-idx="' + idx + '" data-stage="' + dataStage + '">'
        + '<div class="stage__name" title="' + escapeHtml(s.name) + '">' + escapeHtml(s.name) + '</div>'
        + '<div class="stage__bar">'
        + '<div class="stage__bar-track"></div>'
        + '<div class="stage__bar-p99" style="width:' + ((s.p99 / max) * 100).toFixed(1) + '%"></div>'
        + '<div class="stage__bar-p95" style="width:' + ((s.p95 / max) * 100).toFixed(1) + '%"></div>'
        + '<div class="stage__bar-p50" style="width:' + ((s.p50 / max) * 100).toFixed(1) + '%"></div>'
        + '</div>'
        + '<div class="stage__nums">'
        + '<span class="ok">' + escapeHtml(formatMs(s.p50)) + '</span> / '
        + '<span class="warn">' + escapeHtml(formatMs(s.p95)) + '</span> / '
        + '<span class="err">' + escapeHtml(formatMs(s.p99)) + '</span>'
        + '</div>'
        + '</div>';
    }).join('');

    attachStageHover(body);

    var tag = $('stage-status-tag');
    if (tag) tag.textContent = state.runActive ? 'live' : (state.report ? 'final' : 'static');
  }

  function attachStageHover(body) {
    if (!body || body._stageHoverAttached) {
      // Already wired up; re-attach handlers on the same parent so they
      // continue to work after innerHTML re-renders.
    }
    body._stageHoverAttached = true;
    var tooltip = ensureChartTooltip();
    body.onmousemove = function (e) {
      var row = e.target.closest && e.target.closest('.stage[data-stage]');
      if (!row) { hideChartTooltip(); return; }
      try {
        var s = JSON.parse(decodeURIComponent(row.getAttribute('data-stage')));
        showChartTooltip(tooltip, e.clientX, e.clientY, [
          { label: 'stage', value: s.name, muted: true },
          { label: 'p50', value: formatMs(s.p50), color: 'var(--ok)' },
          { label: 'p95', value: formatMs(s.p95), color: 'var(--warn)' },
          { label: 'p99', value: formatMs(s.p99), color: 'var(--err)' },
          { label: 'share of median attempt', value: s.share.toFixed(1) + '%', muted: true },
        ]);
      } catch (err) {
        hideChartTooltip();
      }
    };
    body.onmouseleave = hideChartTooltip;
  }

  function stageSummary() {
    // Prefer the warmup metadata percentiles (definitive at end of run)
    if (state.warmup && state.warmup.stage_duration_percentiles && Object.keys(state.warmup.stage_duration_percentiles).length) {
      return Object.keys(state.warmup.stage_duration_percentiles).map(function (name) {
        var v = state.warmup.stage_duration_percentiles[name] || {};
        return { name: name, p50: +v.p50 || 0, p95: +v.p95 || 0, p99: +v.p99 || 0 };
      });
    }
    // Else compute live from per-attempt samples
    var names = Object.keys(state.stageSamples);
    return names.map(function (name) {
      var arr = state.stageSamples[name].slice().sort(function (a, b) { return a - b; });
      return { name: name, p50: percentile(arr, 0.5), p95: percentile(arr, 0.95), p99: percentile(arr, 0.99) };
    });
  }

  // ----------------------------------------------------------------
  // Attempts grid
  // ----------------------------------------------------------------
  function ensureAttempts() {
    var planned = (state.warmup && state.warmup.planned_attempts)
                  || (state.liveProgress && state.liveProgress.planned_attempts)
                  || (state.report && state.report.overall_attempts)
                  || 0;
    if (!planned) { state.attempts = []; return; }
    if (state.attempts.length !== planned) {
      // Resize while preserving existing
      var prev = state.attempts;
      state.attempts = [];
      for (var i = 0; i < planned; i++) {
        state.attempts.push(prev[i] || { n: i + 1, status: 'pending', durationS: null, message: null });
      }
    }
  }

  function applyAttemptResult(res) {
    if (!res || typeof res.attempt_number !== 'number') return;
    var idx = res.attempt_number - 1;
    if (idx < 0) return;
    while (state.attempts.length <= idx) {
      state.attempts.push({ n: state.attempts.length + 1, status: 'pending', durationS: null });
    }
    var prev = state.attempts[idx];
    var wasTerminal = prev && (prev.status === 'ok' || prev.status === 'warn' || prev.status === 'err' || prev.status === 'skipped');
    var status = attemptStatusFromResult(res);
    state.attempts[idx] = {
      n: res.attempt_number,
      status: status,
      durationS: res.duration_seconds || 0,
      message: (res.conversation && res.conversation.find(function (m) { return (m.role && (m.role.value || m.role)) === 'user'; })) || null,
      error: res.error || null,
      explanation: res.explanation || '',
      conversation: res.conversation || [],
      stages: res.warmup_stage_durations_ms || {},
      raw: res,
    };
    // Stage samples — only push once per attempt to avoid skewing percentiles when
    // the same attempt re-arrives via the /run/status progress window.
    if (!wasTerminal && res.warmup_stage_durations_ms) {
      Object.keys(res.warmup_stage_durations_ms).forEach(function (k) {
        if (!state.stageSamples[k]) state.stageSamples[k] = [];
        state.stageSamples[k].push(+res.warmup_stage_durations_ms[k] || 0);
        if (state.stageSamples[k].length > 200) state.stageSamples[k].splice(0, state.stageSamples[k].length - 200);
      });
    }
  }

  function applyAttemptInProgress(attemptNumber) {
    if (!attemptNumber) return;
    var idx = attemptNumber - 1;
    while (state.attempts.length <= idx) {
      state.attempts.push({ n: state.attempts.length + 1, status: 'pending', durationS: null });
    }
    if (state.attempts[idx].status === 'pending') {
      state.attempts[idx].status = 'running';
    }
  }

  function renderAttemptsGrid() {
    var grid = $('attempts-grid');
    if (!grid) return;
    ensureAttempts();
    if (!state.attempts.length) {
      grid.innerHTML = '<div class="placeholder">No attempts queued yet — configure and start a warm-up.</div>';
      updateAttemptStats();
      return;
    }
    var html = state.attempts.map(function (a) {
      var dur = a.durationS != null ? ' · ' + (+a.durationS).toFixed(2) + 's' : '';
      return '<button class="atmp atmp--' + a.status + '" data-attempt-n="' + a.n + '" aria-label="Attempt ' + a.n + '">'
        + '<span class="atmp__tip">#' + a.n + ' · ' + a.status + dur + '</span>'
        + '</button>';
    }).join('');
    grid.innerHTML = html;
    updateAttemptStats();
  }

  function patchAttemptCell(n) {
    var grid = $('attempts-grid');
    if (!grid) return;
    var idx = n - 1;
    if (idx < 0 || idx >= state.attempts.length) return;
    var btn = grid.querySelector('[data-attempt-n="' + n + '"]');
    if (!btn) {
      renderAttemptsGrid();
      return;
    }
    var a = state.attempts[idx];
    btn.className = 'atmp atmp--' + a.status;
    var tip = btn.querySelector('.atmp__tip');
    if (tip) {
      var dur = a.durationS != null ? ' · ' + (+a.durationS).toFixed(2) + 's' : '';
      tip.textContent = '#' + a.n + ' · ' + a.status + dur;
    }
  }

  function updateAttemptStats() {
    var stats = { ok: 0, warn: 0, err: 0, running: 0, pending: 0, skipped: 0 };
    state.attempts.forEach(function (a) { stats[a.status] = (stats[a.status] || 0) + 1; });
    state.attemptStats = stats;
    var meta = $('attempts-meta');
    if (meta) meta.textContent = state.attempts.length + ' cells';
    ['ok', 'warn', 'err', 'running', 'pending'].forEach(function (k) {
      var el = $('lg-' + k);
      if (el) el.textContent = stats[k] || 0;
    });
  }

  // ----------------------------------------------------------------
  // Diagnostics feed
  // ----------------------------------------------------------------
  function eventLevel(ev) {
    var t = ev.event_type || '';
    if (t === 'attempt_completed' && ev.success === false) return 'err';
    if (t === 'attempt_completed' && ev.success === true) return 'ok';
    if (t === 'attempt_status') return 'info';
    if (t === 'suite_started' || t === 'scenario_started') return 'info';
    if (t === 'suite_completed' || t === 'scenario_completed') return 'ok';
    if (ev.message && /timeout|fail|error/i.test(ev.message)) return 'err';
    return 'info';
  }

  function eventTag(ev) {
    var t = String(ev.event_type || '').replace('attempt_', '').replace('suite_', '').replace('scenario_', '');
    return t || 'event';
  }

  function renderDiagnosticsFeed() {
    var feed = $('diagnostics-feed');
    if (!feed) return;
    var events = state.progressEvents.slice().reverse();
    var counts = { all: events.length, info: 0, ok: 0, warn: 0, err: 0 };
    events.forEach(function (ev) { var lvl = eventLevel(ev); counts[lvl] = (counts[lvl] || 0) + 1; });
    Object.keys(counts).forEach(function (k) {
      var el = document.querySelector('[data-count="' + k + '"]');
      if (el) el.textContent = counts[k] || 0;
    });

    var filter = state.feedFilter;
    var filtered = filter === 'all' ? events : events.filter(function (ev) { return eventLevel(ev) === filter; });
    if (!filtered.length) {
      feed.innerHTML = '<div class="feed__empty">No events match this filter yet.</div>';
      return;
    }
    feed.innerHTML = filtered.slice(0, 200).map(function (ev, i) {
      var lvl = eventLevel(ev);
      return '<div class="feed__item ' + (i === 0 ? 'new ' : '') + 'feed__item--' + lvl + '">'
        + '<span class="feed__time">' + escapeHtml(formatTimeOfDay(ev.emitted_at)) + '</span>'
        + '<span class="feed__tag">' + escapeHtml(eventTag(ev)) + '</span>'
        + '<span class="feed__msg">' + escapeHtml(ev.message || '') + '</span>'
        + '</div>';
    }).join('');

    // Also update the SSR diagnostics list (mirrors recent events)
    var ssrList = $('live-diagnostics-list');
    if (ssrList) {
      var recent = filtered.slice(0, 25);
      if (!recent.length) {
        ssrList.innerHTML = '<li class="empty">No diagnostics emitted for this page view yet.</li>';
      } else {
        ssrList.innerHTML = recent.map(function (ev) {
          return '<li>'
            + '<span class="diagnostic-time">' + escapeHtml(ev.emitted_at || '') + '</span>'
            + '<span class="pill">' + escapeHtml(ev.event_type || 'event') + '</span>'
            + '<span>' + escapeHtml(ev.message || '') + '</span>'
            + '</li>';
        }).join('');
      }
    }
  }

  // ----------------------------------------------------------------
  // History + Schedule
  // ----------------------------------------------------------------
  function renderHistoryTable() {
    var body = $('history-tbody');
    if (!body) return;
    if (!state.history.length) {
      body.innerHTML = '<tr><td colspan="7" class="placeholder">No local warm-up history yet.</td></tr>';
      return;
    }
    body.innerHTML = state.history.map(function (entry) {
      var rate = (entry.overall_success_rate || 0) * 100;
      var rateBg = rate > 95 ? 'var(--ok)' : rate > 85 ? 'var(--warn)' : 'var(--err)';
      var current = entry.run_id === state.viewingHistoryRunId ? ' class="current"' : '';
      return '<tr data-history-run-id="' + escapeHtml(entry.run_id) + '"' + current + '>'
        + '<td class="id">' + escapeHtml(entry.run_id || '') + '</td>'
        + '<td>' + escapeHtml(entry.timestamp || '') + '</td>'
        + '<td>' + escapeHtml(String(entry.completed_attempts || entry.overall_attempts || 0)) + '/' + escapeHtml(String(entry.planned_attempts || entry.overall_attempts || 0)) + '</td>'
        + '<td><span class="mini-bar"><i style="width:' + rate.toFixed(1) + '%;background:' + rateBg + '"></i></span><span class="rate' + (rate < 90 ? ' bad' : '') + '">' + rate.toFixed(1) + '%</span></td>'
        + '<td>' + (entry.attempts_per_second ? (+entry.attempts_per_second).toFixed(3) : '—') + '</td>'
        + '<td>' + (entry.duration_seconds ? formatDuration(entry.duration_seconds) : '—') + '</td>'
        + '<td><span class="tag">' + escapeHtml(entry.trigger_source || 'manual') + '</span></td>'
        + '</tr>';
    }).join('');
  }

  function renderScheduleStrip() {
    var box = $('schedule-strip');
    if (!box) return;
    var html = '';
    state.schedules.forEach(function (s) {
      var suite = ((s.run_request || {}).suite_spec || {}).suite_name || 'AVA Spec Warm Up Suite';
      html += '<div class="sched-card">'
        + '<div class="meta">' + escapeHtml(s.status || 'active') + '</div>'
        + '<div class="when">' + escapeHtml(s.schedule_label || s.cadence || '—') + '</div>'
        + '<div class="what">' + escapeHtml(suite) + '</div>'
        + '<div class="meta">' + (s.next_run_utc ? 'Next ' + escapeHtml(s.next_run_utc) : '—') + '</div>'
        + '</div>';
    });
    html += '<button class="sched-card empty" data-action="configure">'
      + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>'
      + '<span style="font-size:12px">Add schedule</span>'
      + '</button>';
    box.innerHTML = html;
  }

  // ----------------------------------------------------------------
  // Config drawer
  // ----------------------------------------------------------------
  function openDrawer(tab) {
    state.drawerOpen = true;
    if (tab) state.drawerTab = tab;
    $('config-drawer').classList.add('open');
    $('config-drawer').setAttribute('aria-hidden', 'false');
    $('drawer-scrim').classList.add('open');
    renderDrawer();
  }
  function closeDrawer() {
    state.drawerOpen = false;
    $('config-drawer').classList.remove('open');
    $('config-drawer').setAttribute('aria-hidden', 'true');
    $('drawer-scrim').classList.remove('open');
  }

  function renderDrawer() {
    $$('.drawer__tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === state.drawerTab);
    });
    var body = $('drawer-body');
    if (!body) return;
    var tab = state.drawerTab;
    var html = '';
    if (tab === 'target') {
      html = renderTargetTab();
    } else if (tab === 'run') {
      html = renderRunTab();
    } else if (tab === 'suite') {
      html = renderSuiteTab();
    } else if (tab === 'schedule') {
      html = renderScheduleTab();
    }
    body.innerHTML = html;
    updateEffectiveRate();
  }

  function renderTargetTab() {
    var cfg = state.cfg;
    return ''
      + '<div class="field"><label class="field__lbl">Deployment ID<span class="req">*</span></label>'
      + '<input class="input mono" data-cfg="deployment_id" value="' + escapeHtml(cfg.deployment_id) + '" placeholder="6f8a2c14-bc3e-4a89-bf12-1d4f3a9c0210">'
      + '<span class="field__hint">Genesys Cloud Open Messaging deployment.</span></div>'
      + '<div class="row">'
      + '<div class="field"><label class="field__lbl">Region<span class="req">*</span></label>'
      + '<select class="select" data-cfg="region">' + state.regions.map(function (r) {
          return '<option value="' + escapeHtml(r) + '"' + (r === cfg.region ? ' selected' : '') + '>' + escapeHtml(r) + '</option>';
        }).join('') + '</select>'
      + '<span class="field__hint">e.g. mypurecloud.com or usw2.pure.cloud</span></div>'
      + '<div class="field"><label class="field__lbl">LLM model</label>'
      + '<input class="input mono" data-cfg="recorded_model" value="' + escapeHtml(cfg.recorded_model) + '" placeholder="gemma4:e4b">'
      + '<span class="field__hint">Local Ollama model executing the warm-up.</span></div>'
      + '</div>';
  }

  function renderRunTab() {
    var cfg = state.cfg;
    var totalSeconds = cfg.attempt_count * cfg.pacing_seconds / Math.max(1, cfg.execution_mode === 'parallel' ? cfg.worker_count : 1);
    return ''
      + '<div class="field"><label class="field__lbl">Attempts</label>'
      + '<div style="display:grid;grid-template-columns:1fr 80px;gap:10px;align-items:center">'
      + '<input class="range" type="range" min="1" max="500" data-cfg="attempt_count" value="' + cfg.attempt_count + '">'
      + '<input class="input mono" style="text-align:right" data-cfg="attempt_count" value="' + cfg.attempt_count + '">'
      + '</div>'
      + '<span class="field__hint">~' + formatDuration(totalSeconds) + ' at ' + cfg.pacing_seconds.toFixed(1) + 's pacing</span></div>'
      + '<div class="field"><label class="field__lbl">Execution mode</label>'
      + '<div class="seg">'
      + ['serial', 'parallel'].map(function (m) {
          return '<button class="seg__opt' + (cfg.execution_mode === m ? ' active' : '') + '" data-cfg="execution_mode" data-value="' + m + '">' + m + '</button>';
        }).join('')
      + '</div></div>'
      + (cfg.execution_mode === 'parallel'
        ? '<div class="field"><label class="field__lbl">Parallel workers</label><div style="display:flex;gap:6px">'
          + [1,2,3,4,5].map(function (n) {
              return '<button class="btn btn--sm" data-cfg="worker_count" data-value="' + n + '" style="min-width:36px;'
                + (cfg.worker_count === n ? 'background:var(--amber-soft);color:var(--amber);border-color:oklch(60% 0.16 60 / 0.4)' : '')
                + '">' + n + '</button>';
            }).join('')
          + '</div><span class="field__hint">Max 5. Adaptive profile may reduce live.</span></div>'
        : '')
      + '<div class="row">'
      + '<div class="field"><label class="field__lbl">Pacing</label><div class="seg">'
      + state.pacingChoices.map(function (p) {
          return '<button class="seg__opt' + (cfg.pacing_seconds === p ? ' active' : '') + '" data-cfg="pacing_seconds" data-value="' + p + '">' + p.toFixed(1) + 's</button>';
        }).join('')
      + '</div><span class="field__hint">Delay between attempts</span></div>'
      + '<div class="field"><label class="field__lbl">Performance profile</label>'
      + '<select class="select" data-cfg="performance_profile"><option value="safe_adaptive" selected>safe_adaptive</option></select>'
      + '<span class="field__hint">Drops pressure when error rate rises</span></div>'
      + '</div>';
  }

  function renderSuiteTab() {
    var cfg = state.cfg;
    var suite = state.suites.find(function (s) { return s.suite_id === cfg.suite_id; }) || state.suites[0] || { suite_name: '—', scenario_name: '—', messages: [] };
    return ''
      + '<div class="field"><label class="field__lbl">Warm-up suite</label>'
      + '<select class="select" data-cfg="suite_id">'
      + state.suites.map(function (s) { return '<option value="' + escapeHtml(s.suite_id) + '"' + (s.suite_id === cfg.suite_id ? ' selected' : '') + '>' + escapeHtml(s.suite_name) + '</option>'; }).join('')
      + '</select>'
      + '<span class="field__hint">Default suite is built in. Custom JSON suites live under warmup_suites/.</span></div>'
      + '<div style="border:1px solid var(--line);border-radius:10px;background:var(--bg-1);padding:12px;margin-top:4px">'
      + '<div style="display:flex;gap:16px;margin-bottom:8px">'
      + '<div><div class="eyebrow">Scenario</div><div style="font-weight:600">' + escapeHtml(suite.scenario_name) + '</div></div>'
      + '<div><div class="eyebrow">Messages</div><div style="font-weight:600">' + (suite.messages || []).length + '</div></div>'
      + '</div>'
      + '<ol style="margin:0;padding-left:18px;color:var(--fg-2);font-size:12.5px;line-height:1.6">'
      + (suite.messages || []).map(function (m) { return '<li><span class="mono" style="color:var(--fg-3)">"</span>' + escapeHtml(m) + '<span class="mono" style="color:var(--fg-3)">"</span></li>'; }).join('')
      + '</ol></div>';
  }

  function renderScheduleTab() {
    var cfg = state.cfg;
    var cadenceOptions = ['hourly','daily','weekly','monthly','interval'];
    var cadenceLabels = { hourly: 'hourly', daily: 'daily', weekly: 'weekly', monthly: 'monthly', interval: 'every N hours' };
    return ''
      + '<div class="field"><label class="field__lbl">Cadence</label><div class="seg">'
      + cadenceOptions.map(function (c) {
          return '<button class="seg__opt' + (cfg.cadence === c ? ' active' : '') + '" data-cfg="cadence" data-value="' + c + '">' + cadenceLabels[c] + '</button>';
        }).join('')
      + '</div></div>'
      + (cfg.cadence === 'interval'
          ? '<div class="field"><label class="field__lbl">Interval (hours)</label><input class="input" type="number" min="1" step="1" data-cfg="cadence_hours" value="' + (cfg.cadence_hours || 3) + '"><span class="field__hint">Run every N hours, anchored to the start date at 00:MM local time.</span></div>'
          : '')
      + '<div class="row">'
      + (cfg.cadence === 'hourly' || cfg.cadence === 'interval'
          ? '<div class="field"><label class="field__lbl">Minute</label><input class="input" type="number" min="0" max="59" data-cfg="minute" value="' + cfg.minute + '"><span class="field__hint">Minute of the hour to fire</span></div>'
          : '<div class="field"><label class="field__lbl">Time</label><input class="input" type="time" data-cfg="time_hhmm" value="' + escapeHtml(cfg.time_hhmm) + '"></div>')
      + '<div class="field"><label class="field__lbl">Timezone</label><input class="input" data-cfg="timezone_name" value="' + escapeHtml(cfg.timezone_name) + '"></div>'
      + '</div>'
      + (cfg.cadence === 'weekly'
          ? '<div class="field"><label class="field__lbl">Weekday</label><select class="select" data-cfg="weekday">'
            + ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map(function (d, i) { return '<option value="' + i + '"' + (cfg.weekday === i ? ' selected' : '') + '>' + d + '</option>'; }).join('')
            + '</select></div>'
          : '')
      + (cfg.cadence === 'monthly'
          ? '<div class="field"><label class="field__lbl">Day of month</label><input class="input" type="number" min="1" max="31" data-cfg="day_of_month" value="' + cfg.day_of_month + '"></div>'
          : '')
      + '<div class="row">'
      + '<div class="field"><label class="field__lbl">Start date</label><input class="input" type="date" data-cfg="start_date" value="' + escapeHtml(cfg.start_date) + '"></div>'
      + '<div class="field"><label class="field__lbl">End date</label><input class="input" type="date" data-cfg="end_date" value="' + escapeHtml(cfg.end_date) + '"></div>'
      + '</div>'
      + '<div style="padding:12px;border:1px dashed var(--line);border-radius:10px;font-size:12px;color:var(--fg-3)">'
      + 'Schedules write to <code class="mono" style="color:var(--amber)">.ava_warmup_history/</code>. Manual runs are not affected.'
      + '</div>';
  }

  function updateEffectiveRate() {
    var el = $('effective-rate');
    if (!el) return;
    var workers = state.cfg.execution_mode === 'parallel' ? state.cfg.worker_count : 1;
    var rate = workers / Math.max(0.5, state.cfg.pacing_seconds);
    el.textContent = rate.toFixed(2);
  }

  // ----------------------------------------------------------------
  // Detail panel
  // ----------------------------------------------------------------
  function openDetail(attemptN) {
    var a = state.attempts[attemptN - 1];
    if (!a) return;
    state.detailAttempt = a;
    var panel = $('detail-panel');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    renderDetail();
  }
  function closeDetail() {
    state.detailAttempt = null;
    $('detail-panel').classList.remove('open');
    $('detail-panel').setAttribute('aria-hidden', 'true');
  }

  function renderDetail() {
    var a = state.detailAttempt;
    if (!a) return;
    var title = $('detail-title');
    var tagKind = a.status === 'ok' ? 'ok' : a.status === 'warn' ? 'warn' : a.status === 'err' ? 'err' : 'amber';
    if (title) {
      title.innerHTML = '<h2 style="margin:0;font-size:18px">#' + a.n + '</h2>'
        + '<span class="tag tag--' + tagKind + '">' + a.status + '</span>'
        + (a.durationS != null ? '<span class="num" style="color:var(--fg-3)">' + (+a.durationS).toFixed(3) + 's</span>' : '');
    }
    var body = $('detail-body');
    if (!body) return;
    var stages = a.stages ? Object.keys(a.stages).map(function (k) {
      return '<div class="stage">'
        + '<div class="stage__name">' + escapeHtml(k) + '</div>'
        + '<div class="stage__bar"><div class="p50" style="width:60%"></div></div>'
        + '<div class="stage__nums">' + (+a.stages[k]).toFixed(1) + ' ms</div>'
        + '</div>';
    }).join('') : '';
    var conv = (a.conversation || []).map(function (m) {
      var role = (m.role && (m.role.value || m.role)) || 'agent';
      return '<div class="conv__msg ' + role + '"><div class="who">' + role + '</div>' + escapeHtml(m.content || '') + (m.timestamp ? '<span class="ts">' + escapeHtml(String(m.timestamp).slice(11, 19)) + '</span>' : '') + '</div>';
    }).join('');
    body.innerHTML = ''
      + '<div class="eyebrow" style="margin-bottom:8px">Stage timings</div>'
      + (stages || '<div class="stage__empty">No stage timings captured.</div>')
      + renderGenesysAttributes(a)
      + '<div class="eyebrow" style="margin-top:18px;margin-bottom:8px">Web Messenger interaction</div>'
      + (a.conversation && a.conversation.length
          ? '<div class="conv">' + conv + '</div>'
          : '<div class="placeholder">No transcript captured.</div>')
      + (a.error
          ? '<div style="margin-top:16px;padding:12px;border:1px solid oklch(58% 0.19 25 / 0.4);background:var(--err-soft);border-radius:10px;font-family:var(--mono);font-size:12px;color:var(--err)">' + escapeHtml(a.error) + '</div>'
          : '');
  }

  function renderGenesysAttributes(a) {
    var raw = (a && a.raw) || {};
    var td = raw.timeout_diagnostics || {};
    var conversationId = raw.conversation_id || td.conversation_id || null;
    var participantId = raw.participant_id || td.participant_id || null;
    var sessionToken = raw.session_token || null;
    var candidates = raw.conversation_id_candidates && raw.conversation_id_candidates.length
      ? raw.conversation_id_candidates
      : (td.conversation_id_candidates || []);
    var runMeta = (state.warmup) || (state.report && state.report.model_warmup_run) || {};
    var deploymentId = runMeta.deployment_id || state.config.gc_deployment_id || '';
    var region = runMeta.region || state.config.gc_region || '';

    function row(label, value, mono) {
      if (value === null || value === undefined || value === '') return '';
      var cls = mono ? 'mono' : '';
      return '<div class="genesys__row">'
        + '<div class="genesys__lbl">' + escapeHtml(label) + '</div>'
        + '<div class="genesys__val ' + cls + '">' + escapeHtml(String(value)) + '</div>'
        + '</div>';
    }

    var rows = ''
      + row('conversationId (interactionId)', conversationId, true)
      + row('participantId', participantId, true)
      + row('sessionToken', sessionToken, true)
      + row('deploymentId', deploymentId, true)
      + row('region', region, false);
    if (Array.isArray(candidates) && candidates.length) {
      rows += '<div class="genesys__row">'
        + '<div class="genesys__lbl">conversationId candidates</div>'
        + '<div class="genesys__val mono">' + candidates.map(escapeHtml).join('<br>') + '</div>'
        + '</div>';
    }
    if (!rows) {
      rows = '<div class="placeholder">No Genesys identifiers were captured for this attempt.</div>';
    }
    return '<div class="eyebrow" style="margin-top:18px;margin-bottom:8px">Genesys attributes</div>'
      + '<div class="genesys">' + rows + '</div>';
  }

  // ----------------------------------------------------------------
  // Network — fetch report + status
  // ----------------------------------------------------------------
  function fetchReport() {
    var url = '/results/export?format=json' + (state.viewingHistoryRunId ? '&history_run_id=' + encodeURIComponent(state.viewingHistoryRunId) : '');
    return fetch(url, { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function fetchStatus() {
    return fetch('/run/status', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function fetchHistory() {
    return fetch('/results/history?limit=50', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function fetchSchedule() {
    return fetch('/run/model_warm_up/schedule/status', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function applyStatus(status) {
    if (!status) return;
    var wasActive = state.runActive;
    state.runActive = !!status.run_active;
    state.stopRequested = !!status.stop_requested;
    // Trust the server: when no run is active, clear the active id so subsequent polls
    // don't keep reporting a stale token.
    state.activeRunId = status.active_run_id || (state.runActive ? state.activeRunId : null);
    state.triggerSource = status.trigger_source || (state.runActive ? state.triggerSource : 'manual');
    // While a run is active the server's live snapshot is authoritative; once it ends
    // we keep the last known snapshot only if it matches the completed run.
    if (state.runActive) {
      state.warmup = status.model_warmup_run || state.warmup;
      state.liveProgress = status.live_progress || state.liveProgress;
    } else {
      if (status.model_warmup_run) state.warmup = status.model_warmup_run;
      if (status.live_progress) state.liveProgress = status.live_progress;
    }
    // Latest progress events: replace
    if (Array.isArray(status.progress)) {
      state.progressEvents = status.progress;
      // Apply attempt results from progress events for accurate grid
      ensureAttempts();
      status.progress.forEach(function (ev) {
        if (ev.event_type === 'attempt_completed' && ev.attempt_result) {
          applyAttemptResult(ev.attempt_result);
        } else if (ev.event_type === 'attempt_started' && ev.attempt_number) {
          applyAttemptInProgress(ev.attempt_number);
        }
      });
      // Update latency series from completed attempts
      rebuildSeriesFromAttempts();
    }
    // When a run just finished, clear the viewing-history pin so the latest run's
    // report becomes the focus instead of an old historical view.
    if (wasActive && !state.runActive) {
      state.viewingHistoryRunId = null;
    }
  }

  function rebuildSeriesFromAttempts() {
    // Build rolling latency p50/p95/p99 across recent completed attempts (window = 30)
    var durations = state.attempts.filter(function (a) { return a.durationS != null && a.status !== 'pending' && a.status !== 'running'; }).map(function (a) { return (+a.durationS || 0) * 1000; });
    if (!durations.length) {
      state.latencySeries = [];
      state.throughputSeries = [];
      return;
    }
    var window = 10;
    var series = [];
    for (var i = window; i <= durations.length; i++) {
      var slice = durations.slice(Math.max(0, i - window), i).slice().sort(function (a, b) { return a - b; });
      series.push({
        t: i,
        p50: percentile(slice, 0.5),
        p95: percentile(slice, 0.95),
        p99: percentile(slice, 0.99),
      });
    }
    // If fewer than window samples, still show one point of progress
    if (!series.length && durations.length > 0) {
      var sortedAll = durations.slice().sort(function (a, b) { return a - b; });
      series.push({ t: 1, p50: percentile(sortedAll, 0.5), p95: percentile(sortedAll, 0.95), p99: percentile(sortedAll, 0.99) });
    }
    state.latencySeries = series.slice(-60);

    // Throughput sparkline: instantaneous attempts/sec proxy = 1 / duration (clamped)
    var thr = durations.map(function (ms) { return 1000 / Math.max(50, ms); }).slice(-40);
    state.throughputSeries = thr;
  }

  function applyReport(report) {
    state.report = report;
    if (report && report.model_warmup_run) {
      state.warmup = report.model_warmup_run;
    }
    if (report && Array.isArray(report.scenario_results)) {
      ensureAttempts();
      report.scenario_results.forEach(function (sc) {
        (sc.attempt_results || []).forEach(applyAttemptResult);
      });
      rebuildSeriesFromAttempts();
    }
  }

  function refreshAll() {
    var pending = [fetchStatus(), fetchHistory(), fetchSchedule()];
    return Promise.all(pending).then(function (results) {
      applyStatus(results[0]);
      if (results[1] && Array.isArray(results[1].runs)) {
        state.history = results[1].runs.map(function (r) {
          var w = r.model_warmup_run || {};
          return {
            run_id: r.run_id,
            timestamp: r.timestamp,
            overall_attempts: r.overall_attempts,
            overall_success_rate: r.overall_success_rate,
            trigger_source: w.trigger_source || 'manual',
            completed_attempts: w.completed_attempts,
            planned_attempts: w.planned_attempts,
            attempts_per_second: w.attempts_per_second,
            duration_seconds: r.duration_seconds || w.duration_seconds,
          };
        });
      }
      if (results[2]) {
        state.scheduleStatus = results[2];
        state.schedules = results[2].scheduled_warmups || [];
      }
      // If a report exists in latest_report path, fetch full
      return fetchReport();
    }).then(function (report) {
      if (report) applyReport(report);
      renderAll();
    });
  }

  // ----------------------------------------------------------------
  // Actions: start, stop, save schedule
  // ----------------------------------------------------------------
  function startRun() {
    var cfg = state.cfg;
    if (!cfg.deployment_id) {
      openDrawer('target');
      flash('Deployment ID is required.', 'err');
      return;
    }
    var payload = {
      deployment_id: cfg.deployment_id,
      region: cfg.region,
      recorded_model: cfg.recorded_model,
      attempt_count: cfg.attempt_count,
      execution_mode: cfg.execution_mode,
      worker_count: cfg.worker_count,
      pacing_seconds: cfg.pacing_seconds,
      performance_profile: cfg.performance_profile,
      suite_id: cfg.suite_id,
    };
    fetch('/run/model_warm_up', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status === 202 && res.body.ok) {
          flash('Warm-up started.', 'ok');
          state.runActive = true;
          state.stopRequested = false;
          state.activeRunId = res.body.run_id || null;
          state.viewingHistoryRunId = null;
          state.selectedSuiteId = cfg.suite_id;
          state.attempts = [];
          state.stageSamples = {};
          state.latencySeries = [];
          state.throughputSeries = [];
          state.report = null;
          state.warmup = null;
          state.liveProgress = null;
          state.progressEvents = [];
          renderAll();
          startPolling();
        } else {
          var errs = (res.body && res.body.errors) || [res.body && res.body.error || 'Unknown error'];
          flash(errs.join(' · '), 'err');
        }
      })
      .catch(function (e) { flash('Network error: ' + e.message, 'err'); });
  }

  function stopRun() {
    state.stopRequested = true;
    renderTopBar();
    renderCockpit();
    fetch('/run/stop', { method: 'POST', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (!body.ok) flash(body.error || 'Stop failed.', 'err');
        else flash('Stop requested. Finalizing…', 'ok');
      })
      .catch(function (e) { flash('Network error: ' + e.message, 'err'); });
  }

  function saveSchedule() {
    var cfg = state.cfg;
    if (!cfg.deployment_id) {
      openDrawer('target');
      flash('Deployment ID is required.', 'err');
      return;
    }
    if (!cfg.end_date) {
      openDrawer('schedule');
      flash('Schedule end date is required.', 'err');
      return;
    }
    var payload = Object.assign({}, cfg);
    // Translate the UI's "interval" cadence to the numeric form the server accepts.
    if (payload.cadence === 'interval') {
      payload.cadence = String(Math.max(1, parseInt(cfg.cadence_hours, 10) || 1));
    }
    delete payload.cadence_hours;
    fetch('/run/model_warm_up/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status === 200 && res.body.ok) {
          state.scheduleStatus = res.body.schedule || state.scheduleStatus;
          state.schedules = (res.body.schedule || {}).scheduled_warmups || state.schedules;
          renderScheduleStrip();
          flash('Schedule saved.', 'ok');
          closeDrawer();
        } else {
          flash(((res.body && res.body.errors) || [res.body && res.body.error || 'Save failed']).join(' · '), 'err');
        }
      })
      .catch(function (e) { flash('Network error: ' + e.message, 'err'); });
  }

  function cancelSchedule() {
    fetch('/run/model_warm_up/schedule/cancel', { method: 'POST', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (body.ok) {
          state.scheduleStatus = body.schedule || {};
          state.schedules = (body.schedule || {}).scheduled_warmups || [];
          renderScheduleStrip();
          flash('Schedule canceled.', 'ok');
        }
      });
  }

  // ----------------------------------------------------------------
  // Flash banner
  // ----------------------------------------------------------------
  var flashTimer = null;
  function flash(message, kind) {
    var existing = document.getElementById('toast-banner');
    if (existing) existing.remove();
    var node = document.createElement('div');
    node.id = 'toast-banner';
    node.className = 'banner ' + (kind === 'err' ? 'banner--err' : 'banner--ok');
    node.style.position = 'fixed';
    node.style.top = '60px';
    node.style.left = '50%';
    node.style.transform = 'translateX(-50%)';
    node.style.zIndex = '90';
    node.style.maxWidth = '520px';
    node.style.boxShadow = 'var(--shadow-2)';
    node.textContent = message;
    document.body.appendChild(node);
    if (flashTimer) clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 4500);
  }

  // ----------------------------------------------------------------
  // Polling
  // ----------------------------------------------------------------
  var pollTimer = null;
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      if (!state.runActive) {
        stopPolling();
        stopDashboardReload();
        // Final refresh once to fetch the report
        refreshAll();
        return;
      }
      refreshAll();
    }, 1500);
    startDashboardReload();
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Full-page reload every 10s while a run is active, so the whole dashboard
  // — including any bits not covered by incremental polling — stays fresh.
  var dashboardReloadTimer = null;
  function startDashboardReload() {
    if (dashboardReloadTimer) return;
    dashboardReloadTimer = setInterval(function () {
      if (!state.runActive) {
        stopDashboardReload();
        return;
      }
      if (state.dashboardReloadPaused) return;
      window.location.reload();
    }, 10000);
  }
  function stopDashboardReload() {
    if (dashboardReloadTimer) { clearInterval(dashboardReloadTimer); dashboardReloadTimer = null; }
  }

  // ----------------------------------------------------------------
  // Event wiring
  // ----------------------------------------------------------------
  function wireEvents() {
    document.addEventListener('click', function (e) {
      var actionEl = e.target.closest('[data-action]');
      if (actionEl) {
        var action = actionEl.getAttribute('data-action');
        if (action === 'start') { e.preventDefault(); startRun(); return; }
        if (action === 'stop') { e.preventDefault(); stopRun(); return; }
        if (action === 'configure') { e.preventDefault(); openDrawer(); return; }
        if (action === 'close-drawer') { e.preventDefault(); closeDrawer(); return; }
        if (action === 'close-detail') { e.preventDefault(); closeDetail(); return; }
        if (action === 'save-schedule') { e.preventDefault(); saveSchedule(); return; }
        if (action === 'start-from-drawer') { e.preventDefault(); closeDrawer(); startRun(); return; }
        if (action === 'theme') { e.preventDefault(); toggleTheme(); return; }
        if (action === 'toggle-auto-refresh') {
          e.preventDefault();
          state.dashboardReloadPaused = !state.dashboardReloadPaused;
          try { localStorage.setItem('ava_warmup.auto_refresh_paused', state.dashboardReloadPaused ? '1' : '0'); } catch (err) {}
          renderTopBar();
          return;
        }
      }

      // Rail nav buttons — scroll/anchor only (no separate routes in SPA)
      var rail = e.target.closest('[data-nav]');
      if (rail) {
        e.preventDefault();
        var nav = rail.getAttribute('data-nav');
        $$('.rail__btn').forEach(function (b) { b.classList.toggle('active', b === rail); });
        var sel = null;
        if (nav === 'cockpit') sel = '#cockpit-mount';
        else if (nav === 'history') sel = '#history-tbody';
        else if (nav === 'schedule') sel = '#schedule-strip';
        else if (nav === 'suites') sel = '#suite-summary-card';
        if (sel) {
          var node = document.querySelector(sel);
          if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
      }

      // Drawer tab change
      var tab = e.target.closest('.drawer__tab');
      if (tab) {
        e.preventDefault();
        state.drawerTab = tab.getAttribute('data-tab');
        renderDrawer();
        return;
      }

      // Drawer config control (seg/btn)
      var cfgEl = e.target.closest('[data-cfg]');
      if (cfgEl && cfgEl.tagName !== 'INPUT' && cfgEl.tagName !== 'SELECT' && cfgEl.hasAttribute('data-value')) {
        e.preventDefault();
        var key = cfgEl.getAttribute('data-cfg');
        var raw = cfgEl.getAttribute('data-value');
        var val = (key === 'pacing_seconds' || key === 'worker_count' || key === 'attempt_count' || key === 'minute' || key === 'day_of_month' || key === 'weekday')
          ? parseFloat(raw) : raw;
        state.cfg[key] = val;
        renderDrawer();
        return;
      }

      // Side tab filter
      var filterEl = e.target.closest('.side__tab[data-filter]');
      if (filterEl) {
        e.preventDefault();
        state.feedFilter = filterEl.getAttribute('data-filter');
        $$('.side__tab').forEach(function (t) { t.classList.toggle('active', t === filterEl); });
        renderDiagnosticsFeed();
        return;
      }

      // Attempt cell click → open detail
      var atmp = e.target.closest('.atmp[data-attempt-n]');
      if (atmp) {
        e.preventDefault();
        openDetail(+atmp.getAttribute('data-attempt-n'));
        return;
      }

      // History row click → view that run
      var histRow = e.target.closest('[data-history-run-id]');
      if (histRow) {
        var runId = histRow.getAttribute('data-history-run-id');
        if (runId) {
          window.location.href = '/results?history_run_id=' + encodeURIComponent(runId);
        }
        return;
      }
    });

    // Drawer scrim closes
    var scrim = $('drawer-scrim');
    if (scrim) scrim.addEventListener('click', closeDrawer);

    // Feed pause toggle
    var pause = $('feed-pause');
    if (pause) pause.addEventListener('change', function (e) {
      state.feedPaused = e.target.checked;
    });

    // Input changes within drawer
    document.addEventListener('input', function (e) {
      var el = e.target.closest('[data-cfg]');
      if (!el) return;
      if (el.tagName !== 'INPUT' && el.tagName !== 'SELECT') return;
      var key = el.getAttribute('data-cfg');
      var v = el.value;
      if (key === 'attempt_count' || key === 'worker_count' || key === 'minute' || key === 'day_of_month' || key === 'weekday') {
        state.cfg[key] = parseInt(v || '0', 10) || 0;
      } else if (key === 'cadence_hours') {
        state.cfg[key] = Math.max(1, parseInt(v || '1', 10) || 1);
      } else if (key === 'pacing_seconds') {
        state.cfg[key] = parseFloat(v) || 1.0;
      } else {
        state.cfg[key] = v;
      }
      // Sync paired inputs (range + number for attempts)
      if (key === 'attempt_count') {
        $$('[data-cfg="attempt_count"]').forEach(function (n) { if (n !== el) n.value = state.cfg.attempt_count; });
      }
      updateEffectiveRate();
      // Re-render dependent tabs immediately
      if (key === 'cadence' || key === 'execution_mode' || key === 'suite_id') {
        renderDrawer();
      }
      // Topbar deployment chip live preview
      if (key === 'deployment_id' || key === 'region') renderTopBar();
    });

    document.addEventListener('change', function (e) {
      var el = e.target.closest('[data-cfg]');
      if (!el || el.tagName !== 'SELECT') return;
      var key = el.getAttribute('data-cfg');
      state.cfg[key] = el.value;
      if (key === 'cadence' || key === 'suite_id') renderDrawer();
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if (t && (t.matches('input, textarea, select') || t.isContentEditable)) return;
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        if (!state.runActive) startRun(); else stopRun();
      } else if (e.key === '.') {
        e.preventDefault();
        if (state.drawerOpen) closeDrawer(); else openDrawer();
      } else if (e.key === 'Escape') {
        if (state.drawerOpen) closeDrawer();
        if (state.detailAttempt) closeDetail();
      }
    });
  }

  function toggleTheme() {
    var root = document.documentElement;
    var current = root.getAttribute('data-theme') || 'dark';
    root.setAttribute('data-theme', current === 'dark' ? 'light' : 'dark');
    try { localStorage.setItem('ava-theme', root.getAttribute('data-theme')); } catch (e) { /* noop */ }
  }

  // ----------------------------------------------------------------
  // Initial render
  // ----------------------------------------------------------------
  function renderAll() {
    renderTopBar();
    renderCockpit();
    renderAttemptsGrid();
    renderLatencyChart();
    renderStageTimings();
    renderDiagnosticsFeed();
    renderHistoryTable();
    renderScheduleStrip();
    updateEffectiveRate();
    attachLatencyHover();
    attachSparkHover();
    var fc = $('failures-card');
    if (fc) {
      var summaries = bootstrap.failure_summaries || [];
      if (state.report && state.report.scenario_results) {
        // Recompute failures from current report
        var counter = {};
        state.report.scenario_results.forEach(function (sc) {
          (sc.attempt_results || []).forEach(function (a) {
            if (a.success || a.skipped) return;
            var msg = a.error || a.explanation || 'Unknown warm-up failure.';
            counter[msg] = (counter[msg] || 0) + 1;
          });
        });
        summaries = Object.keys(counter).map(function (k) { return { message: k, count: counter[k] }; }).sort(function (a, b) { return b.count - a.count; }).slice(0, 5);
      }
      if (summaries && summaries.length) {
        fc.hidden = false;
        fc.querySelector('.tag').textContent = summaries.reduce(function (s, r) { return s + r.count; }, 0) + ' total';
        var bd = fc.querySelector('.card__bd');
        if (bd) bd.innerHTML = summaries.map(function (r, i) {
          return '<div style="display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:center;padding:10px 14px;' + (i ? 'border-top:1px solid var(--line)' : '') + '">'
            + '<span class="num" style="color:var(--err);font-size:18px;font-weight:600">' + r.count + '×</span>'
            + '<span style="color:var(--fg-2);font-size:12.5px">' + escapeHtml(r.message) + '</span>'
            + '</div>';
        }).join('');
      } else {
        fc.hidden = true;
      }
    }
  }

  // Persist + restore theme
  try {
    var saved = localStorage.getItem('ava-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) { /* noop */ }

  // ----------------------------------------------------------------
  // Boot
  // ----------------------------------------------------------------
  function boot() {
    wireEvents();

    // Seed attempts from bootstrap report if any
    if (state.report) applyReport(state.report);
    // Apply any seeded progress events
    state.progressEvents.forEach(function (ev) {
      if (ev.event_type === 'attempt_completed' && ev.attempt_result) {
        ensureAttempts();
        applyAttemptResult(ev.attempt_result);
      } else if (ev.event_type === 'attempt_started' && ev.attempt_number) {
        ensureAttempts();
        applyAttemptInProgress(ev.attempt_number);
      }
    });
    rebuildSeriesFromAttempts();
    renderAll();

    // Always do an initial refresh to pull latest report + history + schedule
    refreshAll();

    if (state.runActive) startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
