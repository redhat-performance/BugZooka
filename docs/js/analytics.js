(function () {
  'use strict';

  /* ─── CONFIG ─── All tunables in one place ──────────────────────────── */
  const CONFIG = {
    storageKey: 'bugzooka-analytics-config',
    defaultIndex: 'bugzooka-telemetry',

    defaultCostInput:  0.00025,
    defaultCostOutput: 0.001,

    minutesSavedPerAnalysis: 30,
    minutesSavedPerPRReview: 45,
    hourlyEngineerCost:      75,

    colors: [
      '#818cf8', '#f472b6', '#34d399', '#fbbf24',
      '#60a5fa', '#f87171', '#a78bfa', '#2dd4bf',
    ],
    successColor: '#22c55e',
    failColor:    '#ef4444',
    warnColor:    '#f59e0b',

    theme: {
      fontFamily:  'Inter',
      labelColor:  '#a1a1aa',
      mutedColor:  '#52525b',
      gridColor:   'rgba(39,39,42,0.5)',
      borderColor: '#27272a',
      bgTooltip:   '#18181b',
      fgTooltip:   '#fafafa',
    },
  };
  /* ──────────────────────────────────────────────────────────────────── */

  const T = CONFIG.theme;

  const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: T.labelColor, font: { family: T.fontFamily, size: 11 } } },
      tooltip: {
        backgroundColor: T.bgTooltip, titleColor: T.fgTooltip, bodyColor: T.labelColor,
        borderColor: T.borderColor, borderWidth: 1, cornerRadius: 6,
        titleFont: { family: T.fontFamily, weight: '600' },
        bodyFont:  { family: 'JetBrains Mono', size: 12 },
      },
    },
    scales: {
      x: { ticks: { color: T.mutedColor, font: { family: T.fontFamily, size: 11 } }, grid: { color: T.gridColor }, border: { color: T.borderColor } },
      y: { ticks: { color: T.mutedColor, font: { family: T.fontFamily, size: 11 } }, grid: { color: T.gridColor }, border: { color: T.borderColor } },
    },
  };

  const doughnutOpts = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      ...chartDefaults.plugins,
      legend: { position: 'bottom', labels: { color: T.labelColor, font: { family: T.fontFamily, size: 12 }, padding: 16 } },
    },
  };

  const charts = {};

  function loadConfig() { try { return JSON.parse(localStorage.getItem(CONFIG.storageKey)) || {}; } catch { return {}; } }
  function saveConfig(cfg) { localStorage.setItem(CONFIG.storageKey, JSON.stringify(cfg)); }

  function getTimeRange() {
    const sel = document.getElementById('time-range').value;
    if (sel === 'custom') {
      const fromVal = document.getElementById('date-from').value;
      const toVal   = document.getElementById('date-to').value;
      const from = fromVal ? new Date(fromVal) : new Date(Date.now() - 6048e5);
      const to   = toVal   ? new Date(new Date(toVal).getTime() + 864e5 - 1) : new Date();
      const days = (to - from) / 864e5;
      return {
        key: 'custom', from: from.toISOString(), to: to.toISOString(),
        interval: days <= 2 ? 'hour' : days > 60 ? 'week' : 'day',
      };
    }
    const ms = { '24h': 864e5, '7d': 6048e5, '30d': 2592e6, '90d': 7776e6, 'all': 3.1536e12 };
    return {
      key: sel,
      from: new Date(Date.now() - (ms[sel] || ms['7d'])).toISOString(),
      to: new Date().toISOString(),
      interval: sel === '24h' ? 'hour' : (sel === '90d' || sel === 'all') ? 'week' : 'day',
    };
  }

  function setStatus(type, msg) {
    document.querySelector('.status-dot').className = 'status-dot status-dot-' + type;
    document.getElementById('status-text').textContent = msg;
    if (type === 'ok') document.getElementById('status-time').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }

  function fmt(n) { if (n == null) return '--'; if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'; if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'; return String(n); }
  function fmtMs(ms) { if (ms == null) return '--'; if (ms < 1000) return ms + 'ms'; return (ms / 1000).toFixed(1) + 's'; }
  function fmtPct(n) { return n == null ? '--' : n.toFixed(1) + '%'; }
  function fmtCost(n) { return n == null ? '$--' : '$' + n.toFixed(4); }
  function shortDate(iso) { const d = new Date(iso); return (d.getMonth() + 1) + '/' + d.getDate(); }
  function getCostPer1k() { const c = loadConfig(); return (parseFloat(c.costInput) || CONFIG.defaultCostInput) + (parseFloat(c.costOutput) || CONFIG.defaultCostOutput); }

  async function queryES(body) {
    const cfg = loadConfig();
    if (!cfg.url) throw new Error('No Elasticsearch URL configured');
    const url = cfg.url.replace(/\/+$/, '') + '/' + (cfg.index || CONFIG.defaultIndex) + '/_search';
    const headers = { 'Content-Type': 'application/json' };
    if (cfg.user && cfg.pass) headers['Authorization'] = 'Basic ' + btoa(cfg.user + ':' + cfg.pass);
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
    if (!res.ok) throw new Error('ES query failed: ' + res.status);
    return res.json();
  }

  async function fetchAllMetrics() {
    const range = getTimeRange();
    const tf = { range: { timestamp: { gte: range.from, lte: range.to } } };
    return queryES({
      size: 0,
      query: { bool: { filter: [tf] } },
      aggs: {
        unique_users: { cardinality: { field: 'user_id' } },
        unique_teams: { cardinality: { field: 'team' } },
        unique_channels: { cardinality: { field: 'channel_id' } },
        success_buckets: { terms: { field: 'success', size: 10 } },
        percentiles: { percentiles: { field: 'duration_ms', percents: [50, 95] } },
        total_tokens: { sum: { field: 'total_tokens' } },
        by_team: { terms: { field: 'team', size: 20 } },
        by_trigger: { terms: { field: 'trigger_type', size: 10 } },
        by_command: { terms: { field: 'command', size: 20 }, aggs: { total_tokens: { sum: { field: 'total_tokens' } } } },
        fail_by_command: { filter: { term: { success: false } }, aggs: { cmds: { terms: { field: 'command', size: 20 } } } },
        over_time: {
          date_histogram: { field: 'timestamp', calendar_interval: range.interval },
          aggs: {
            success_count: { filter: { term: { success: true } } },
            fail_count:    { filter: { term: { success: false } } },
            p50: { percentiles: { field: 'duration_ms', percents: [50] } },
            p95: { percentiles: { field: 'duration_ms', percents: [95] } },
            tokens: { sum: { field: 'total_tokens' } },
          },
        },
        commands_over_time: { date_histogram: { field: 'timestamp', calendar_interval: range.interval }, aggs: { cmds: { terms: { field: 'command', size: 10 } } } },
        latency_by_cmd: { terms: { field: 'command', size: 20 }, aggs: { p50: { percentiles: { field: 'duration_ms', percents: [50] } }, p95: { percentiles: { field: 'duration_ms', percents: [95] } } } },
        team_details: {
          terms: { field: 'team', size: 20 },
          aggs: {
            success:      { filter: { term: { success: true } } },
            fail:         { filter: { term: { success: false } } },
            p50_latency:  { percentiles: { field: 'duration_ms', percents: [50] } },
            total_tokens: { sum: { field: 'total_tokens' } },
            unique_users: { cardinality: { field: 'user_id' } },
            by_trigger:   { terms: { field: 'trigger_type', size: 5 } },
            cmd_detail: {
              terms: { field: 'command', size: 10 },
              aggs: {
                ok:   { filter: { term: { success: true } } },
                fail: { filter: { term: { success: false } } },
                avg_latency: { avg: { field: 'duration_ms' } },
                tokens: { sum: { field: 'total_tokens' } },
              },
            },
          },
        },
      },
    });
  }

  async function tryStaticData() {
    try { const r = await fetch('analytics-data.json?t=' + Date.now()); return r.ok ? r.json() : null; }
    catch { return null; }
  }

  function parseESResponse(raw) {
    const a = raw.aggregations;
    const sb = a.success_buckets.buckets;
    const ok  = sb.find(b => b.key_as_string === 'true')?.doc_count || 0;
    const bad = sb.find(b => b.key_as_string === 'false')?.doc_count || 0;
    const total = ok + bad;

    return {
      kpis: {
        total_requests: total, unique_users: a.unique_users.value, unique_teams: a.unique_teams.value,
        active_channels: a.unique_channels.value,
        success_rate: total ? (ok / total) * 100 : 0, success_count: ok, fail_count: bad,
        p50_latency_ms: a.percentiles.values['50.0'], p95_latency_ms: a.percentiles.values['95.0'],
        total_tokens: a.total_tokens.value,
      },
      usage: {
        by_team:    a.by_team.buckets.map(b => ({ team: b.key, count: b.doc_count })),
        by_trigger: a.by_trigger.buckets.map(b => ({ trigger: b.key, count: b.doc_count })),
      },
      features: {
        commands: a.by_command.buckets.map(b => ({ command: b.key, count: b.doc_count, total_tokens: b.total_tokens.value })),
        commands_over_time: a.commands_over_time.buckets.map(b => ({ date: b.key_as_string, commands: b.cmds.buckets.map(c => ({ command: c.key, count: c.doc_count })) })),
      },
      reliability: {
        fail_by_command: a.fail_by_command.cmds.buckets.map(b => ({ command: b.key, count: b.doc_count })),
        over_time: a.over_time.buckets.map(b => ({ date: b.key_as_string, success: b.success_count.doc_count, fail: b.fail_count.doc_count })),
      },
      performance: {
        over_time: a.over_time.buckets.map(b => ({ date: b.key_as_string, p50: b.p50.values['50.0'], p95: b.p95.values['95.0'] })),
        by_command: a.latency_by_cmd.buckets.map(b => ({ command: b.key, p50: b.p50.values['50.0'], p95: b.p95.values['95.0'] })),
      },
      tokens: {
        over_time:  a.over_time.buckets.map(b => ({ date: b.key_as_string, tokens: b.tokens.value })),
        by_command: a.by_command.buckets.map(b => ({ command: b.key, count: b.doc_count, tokens: b.total_tokens.value })),
      },
      team_details: a.team_details.buckets.map(b => ({
        team: b.key, total: b.doc_count,
        success: b.success.doc_count, fail: b.fail.doc_count,
        success_rate: b.doc_count ? (b.success.doc_count / b.doc_count) * 100 : 0,
        p50_latency: b.p50_latency.values['50.0'],
        tokens: b.total_tokens.value, users: b.unique_users.value,
        trigger: b.by_trigger.buckets.map(t => t.key).join(', ') || 'n/a',
        command_details: b.cmd_detail.buckets.map(c => ({
          command: c.key, total: c.doc_count,
          success: c.ok.doc_count, fail: c.fail.doc_count,
          success_rate: c.doc_count ? (c.ok.doc_count / c.doc_count) * 100 : 0,
          avg_latency: c.avg_latency.value, tokens: c.tokens.value,
        })),
      })),
    };
  }

  function renderKPIs(data) {
    const k = data.kpis;
    document.getElementById('kpi-requests').textContent = fmt(k.total_requests);
    document.getElementById('kpi-users').textContent    = fmt(k.unique_users);
    document.getElementById('kpi-teams').textContent    = fmt(k.unique_teams);
    document.getElementById('kpi-success').textContent  = fmtPct(k.success_rate);
    document.getElementById('kpi-latency').textContent  = fmtMs(k.p50_latency_ms);
    document.getElementById('kpi-tokens').textContent   = fmt(k.total_tokens);

    document.getElementById('kpi-requests-sub').textContent = k.active_channels + ' channels';
    document.getElementById('kpi-users-sub').textContent = 'distinct user IDs';
    document.getElementById('kpi-teams-sub').textContent = data.usage.by_team.map(t => t.team).join(', ');
    const sEl = document.getElementById('kpi-success-sub');
    sEl.textContent = k.fail_count + ' failures';
    sEl.className = 'kpi-sub ' + (k.success_rate >= 90 ? 'kpi-sub-good' : 'kpi-sub-bad');
    document.getElementById('kpi-latency-sub').textContent = 'p95: ' + fmtMs(k.p95_latency_ms);
    document.getElementById('kpi-tokens-sub').textContent = '~' + fmtCost((k.total_tokens / 1000) * getCostPer1k());
  }

  function renderValueSection(data) {
    const cmds = data.features.commands;
    const analyzeCount = cmds.filter(c => ['auto_analyze', 'auto_viz'].includes(c.command)).reduce((s, c) => s + c.count, 0);
    const prCount      = cmds.find(c => c.command === 'analyze_pr')?.count || 0;
    const summaryCount = cmds.filter(c => ['summarize', 'perf_summary'].includes(c.command)).reduce((s, c) => s + c.count, 0);
    const hoursSaved   = ((analyzeCount * CONFIG.minutesSavedPerAnalysis) + (prCount * CONFIG.minutesSavedPerPRReview)) / 60;
    const days         = data.reliability.over_time.length || 1;

    document.getElementById('val-analyses').textContent   = fmt(analyzeCount);
    document.getElementById('val-pr-reviews').textContent = fmt(prCount);
    document.getElementById('val-summaries').textContent  = fmt(summaryCount);
    document.getElementById('val-hours').textContent      = '~' + Math.round(hoursSaved).toLocaleString();
    document.getElementById('val-savings').textContent    = '~$' + Math.round(hoursSaved * CONFIG.hourlyEngineerCost).toLocaleString();
    document.getElementById('val-per-day').textContent    = fmt(Math.round(data.kpis.total_requests / days));

    document.getElementById('val-analyses-sub').textContent   = 'auto_analyze + auto_viz';
    document.getElementById('val-pr-reviews-sub').textContent = 'analyze_pr invocations';
    document.getElementById('val-summaries-sub').textContent  = 'summarize + perf_summary';
    document.getElementById('val-hours-sub').textContent      = 'estimate: ' + CONFIG.minutesSavedPerAnalysis + ' min/analysis, ' + CONFIG.minutesSavedPerPRReview + ' min/PR';
    document.getElementById('val-savings-sub').textContent    = 'estimate @$' + CONFIG.hourlyEngineerCost + '/hr — adjust in settings';
    document.getElementById('val-per-day-sub').textContent    = 'across ' + days + ' days';
  }

  function renderSuccessBreakdown(data) {
    const k = data.kpis;
    document.getElementById('gauge-pct').textContent  = fmtPct(k.success_rate);
    document.getElementById('gauge-ok').textContent   = k.success_count;
    document.getElementById('gauge-fail').textContent  = k.fail_count;
    const bar = document.getElementById('gauge-bar');
    bar.style.width = k.success_rate + '%';
    bar.style.background = k.success_rate >= 90 ? CONFIG.successColor : k.success_rate >= 70 ? CONFIG.warnColor : CONFIG.failColor;
  }

  function renderTeamCards(data) {
    const container = document.getElementById('team-cards');
    container.innerHTML = '';
    for (const t of (data.team_details || [])) {
      const sr = t.success_rate;
      const srClass = sr >= 90 ? 'team-good' : sr >= 70 ? 'team-warn' : 'team-bad';

      let cmdRows = '';
      for (const c of (t.command_details || [])) {
        const csr = c.success_rate;
        const csrClass = csr >= 90 ? 'team-good' : csr >= 70 ? 'team-warn' : 'team-bad';
        const barW = c.total > 0 ? Math.round((c.success / c.total) * 100) : 0;
        cmdRows +=
          '<tr>' +
          '<td>' + c.command + '</td>' +
          '<td>' + c.total + '</td>' +
          '<td>' + c.success + '</td>' +
          '<td>' + c.fail + '</td>' +
          '<td><span class="team-sr ' + csrClass + '">' + fmtPct(csr) + '</span></td>' +
          '<td><div class="mini-bar-track"><div class="mini-bar-fill" style="width:' + barW + '%;background:' +
            (csr >= 90 ? CONFIG.successColor : csr >= 70 ? CONFIG.warnColor : CONFIG.failColor) + '"></div></div></td>' +
          '<td>' + fmtMs(c.avg_latency) + '</td>' +
          '</tr>';
      }

      const card = document.createElement('div');
      card.className = 'team-detail-card';
      card.innerHTML =
        '<div class="tdc-header">' +
          '<h3 class="tdc-name">' + t.team + '</h3>' +
          '<span class="team-sr ' + srClass + ' tdc-badge">' + fmtPct(sr) + ' success</span>' +
        '</div>' +
        '<div class="tdc-stats">' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + t.total + '</span><span class="tdc-stat-lbl">requests</span></div>' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + t.users + '</span><span class="tdc-stat-lbl">users</span></div>' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + t.success + '</span><span class="tdc-stat-lbl tdc-ok">success</span></div>' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + t.fail + '</span><span class="tdc-stat-lbl tdc-fail">failures</span></div>' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + fmtMs(t.p50_latency) + '</span><span class="tdc-stat-lbl">p50 latency</span></div>' +
          '<div class="tdc-stat"><span class="tdc-stat-val">' + fmt(t.tokens) + '</span><span class="tdc-stat-lbl">tokens</span></div>' +
        '</div>' +
        '<div class="tdc-bar-row">' +
          '<div class="tdc-bar-track">' +
            '<div class="tdc-bar-ok" style="width:' + sr + '%"></div>' +
            '<div class="tdc-bar-fail" style="width:' + (100 - sr) + '%"></div>' +
          '</div>' +
          '<span class="tdc-bar-label">' + t.success + ' ok / ' + t.fail + ' fail</span>' +
        '</div>' +
        '<table class="tdc-table">' +
          '<thead><tr><th>Command</th><th>Total</th><th>OK</th><th>Fail</th><th>Rate</th><th></th><th>Latency</th></tr></thead>' +
          '<tbody>' + cmdRows + '</tbody>' +
        '</table>';
      container.appendChild(card);
    }
  }

  function makeOrUpdate(id, config) {
    if (charts[id]) { charts[id].data = config.data; if (config.options) charts[id].options = config.options; charts[id].update(); return; }
    const ctx = document.getElementById(id);
    if (!ctx) return;
    charts[id] = new Chart(ctx, config);
  }

  function renderCharts(data) {
    makeOrUpdate('chart-requests-time', {
      type: 'line',
      data: {
        labels: data.reliability.over_time.map(b => shortDate(b.date)),
        datasets: [{ label: 'Requests', data: data.reliability.over_time.map(b => b.success + b.fail), borderColor: CONFIG.colors[0], backgroundColor: CONFIG.colors[0] + '22', fill: true, tension: 0.3, pointRadius: 3 }],
      },
      options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
    });

    makeOrUpdate('chart-team-usage', {
      type: 'bar',
      data: {
        labels: data.usage.by_team.map(t => t.team),
        datasets: [{ label: 'Requests', data: data.usage.by_team.map(t => t.count), backgroundColor: data.usage.by_team.map((_, i) => CONFIG.colors[i % CONFIG.colors.length]), borderRadius: 4, maxBarThickness: 48 }],
      },
      options: { ...chartDefaults, indexAxis: 'y', plugins: { ...chartDefaults.plugins, legend: { display: false } } },
    });

    makeOrUpdate('chart-team-share', {
      type: 'doughnut',
      data: {
        labels: data.usage.by_team.map(t => t.team),
        datasets: [{ data: data.usage.by_team.map(t => t.count), backgroundColor: data.usage.by_team.map((_, i) => CONFIG.colors[i % CONFIG.colors.length]), borderWidth: 0 }],
      },
      options: doughnutOpts,
    });

    makeOrUpdate('chart-triggers', {
      type: 'doughnut',
      data: {
        labels: data.usage.by_trigger.map(t => t.trigger),
        datasets: [{ data: data.usage.by_trigger.map(t => t.count), backgroundColor: [CONFIG.colors[4], CONFIG.colors[3]], borderWidth: 0 }],
      },
      options: doughnutOpts,
    });

    makeOrUpdate('chart-commands', {
      type: 'doughnut',
      data: {
        labels: data.features.commands.map(c => c.command),
        datasets: [{ data: data.features.commands.map(c => c.count), backgroundColor: data.features.commands.map((_, i) => CONFIG.colors[i % CONFIG.colors.length]), borderWidth: 0 }],
      },
      options: doughnutOpts,
    });

    const allCmds = [...new Set(data.features.commands.map(c => c.command))];
    makeOrUpdate('chart-commands-time', {
      type: 'bar',
      data: {
        labels: data.features.commands_over_time.map(b => shortDate(b.date)),
        datasets: allCmds.map((cmd, i) => ({
          label: cmd,
          data: data.features.commands_over_time.map(b => { const f = b.commands.find(c => c.command === cmd); return f ? f.count : 0; }),
          backgroundColor: CONFIG.colors[i % CONFIG.colors.length] + '88',
          borderColor: CONFIG.colors[i % CONFIG.colors.length], borderWidth: 1, stack: 'stack',
        })),
      },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, stacked: true }, y: { ...chartDefaults.scales.y, stacked: true } } },
    });

    const ot = data.reliability.over_time;
    makeOrUpdate('chart-success-time', {
      type: 'bar',
      data: {
        labels: ot.map(b => shortDate(b.date)),
        datasets: [
          { label: 'Success', data: ot.map(b => b.success), backgroundColor: CONFIG.successColor + 'cc', borderRadius: 4, stack: 'sf' },
          { label: 'Failure', data: ot.map(b => b.fail),    backgroundColor: CONFIG.failColor + 'cc',    borderRadius: 4, stack: 'sf' },
        ],
      },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, stacked: true }, y: { ...chartDefaults.scales.y, stacked: true } } },
    });

    const failTeams = (data.team_details || []).filter(t => t.fail > 0).sort((a, b) => b.fail - a.fail);
    makeOrUpdate('chart-fail-by-team', {
      type: 'bar',
      data: {
        labels: failTeams.map(t => t.team),
        datasets: [{ label: 'Failures', data: failTeams.map(t => t.fail), backgroundColor: CONFIG.failColor + 'aa', borderColor: CONFIG.failColor, borderWidth: 1, borderRadius: 4, maxBarThickness: 48 }],
      },
      options: { ...chartDefaults, indexAxis: 'y', plugins: { ...chartDefaults.plugins, legend: { display: false } } },
    });

    const failCmds = (data.reliability.fail_by_command || []).filter(c => c.count > 0);
    if (failCmds.length) {
      makeOrUpdate('chart-fail-by-cmd', {
        type: 'bar',
        data: {
          labels: failCmds.map(c => c.command),
          datasets: [{ label: 'Failures', data: failCmds.map(c => c.count), backgroundColor: CONFIG.colors.map(c => c + 'aa'), borderRadius: 4, maxBarThickness: 48 }],
        },
        options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
      });
    }

    const pot = data.performance.over_time;
    makeOrUpdate('chart-latency-time', {
      type: 'line',
      data: {
        labels: pot.map(b => shortDate(b.date)),
        datasets: [
          { label: 'p50', data: pot.map(b => b.p50 != null ? Math.round(b.p50 / 1000) : null), borderColor: '#34d399', fill: false, tension: 0.3, pointRadius: 3 },
          { label: 'p95', data: pot.map(b => b.p95 != null ? Math.round(b.p95 / 1000) : null), borderColor: '#fbbf24', fill: false, tension: 0.3, pointRadius: 3 },
        ],
      },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, title: { display: true, text: 'seconds', color: T.mutedColor, font: { family: T.fontFamily, size: 11 } } } } },
    });

    const pc = data.performance.by_command;
    makeOrUpdate('chart-latency-cmd', {
      type: 'bar',
      data: {
        labels: pc.map(c => c.command),
        datasets: [
          { label: 'p50', data: pc.map(c => c.p50 != null ? Math.round(c.p50 / 1000) : 0), backgroundColor: '#34d399aa', borderRadius: 4, maxBarThickness: 32 },
          { label: 'p95', data: pc.map(c => c.p95 != null ? Math.round(c.p95 / 1000) : 0), backgroundColor: '#fbbf24aa', borderRadius: 4, maxBarThickness: 32 },
        ],
      },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, title: { display: true, text: 'seconds', color: T.mutedColor, font: { family: T.fontFamily, size: 11 } } } } },
    });

    makeOrUpdate('chart-tokens-time', {
      type: 'bar',
      data: {
        labels: data.tokens.over_time.map(b => shortDate(b.date)),
        datasets: [{ label: 'Tokens', data: data.tokens.over_time.map(b => b.tokens), backgroundColor: CONFIG.colors[6] + 'aa', borderColor: CONFIG.colors[6], borderWidth: 1, borderRadius: 4 }],
      },
      options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
    });

    makeOrUpdate('chart-tokens-cmd', {
      type: 'bar',
      data: {
        labels: data.tokens.by_command.map(c => c.command),
        datasets: [{ label: 'Tokens', data: data.tokens.by_command.map(c => c.tokens), backgroundColor: data.tokens.by_command.map((_, i) => CONFIG.colors[i % CONFIG.colors.length] + 'aa'), borderRadius: 4, maxBarThickness: 48 }],
      },
      options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
    });
  }

  function renderCostTable(data) {
    const costPer1k = getCostPer1k();
    let totalCost = 0;
    const tbody = document.getElementById('cost-table-body');
    tbody.innerHTML = '';
    for (const cmd of data.tokens.by_command) {
      const cost = (cmd.tokens / 1000) * costPer1k;
      totalCost += cost;
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + cmd.command + '</td><td>' + fmt(cmd.count) + '</td><td>' + fmt(cmd.tokens) + '</td><td>' + fmt(cmd.count > 0 ? Math.round(cmd.tokens / cmd.count) : 0) + '</td><td>' + fmtCost(cost) + '</td>';
      tbody.appendChild(tr);
    }
    document.getElementById('cost-total').textContent = fmtCost(totalCost);
    document.getElementById('cost-per-request').textContent = fmtCost(totalCost / (data.kpis.total_requests || 1));
    document.getElementById('cost-per-day').textContent = fmtCost(totalCost / (data.tokens.over_time.length || 1));
  }

  function renderAll(data) {
    renderKPIs(data);
    renderValueSection(data);
    renderSuccessBreakdown(data);
    renderTeamCards(data);
    renderCharts(data);
    renderCostTable(data);
  }

  async function loadData() {
    setStatus('pending', 'Loading analytics data...');
    document.getElementById('refresh-btn').classList.add('loading');

    try {
      const cfg = loadConfig();
      let data;
      if (cfg.url) {
        data = parseESResponse(await fetchAllMetrics());
        setStatus('ok', 'Connected to Elasticsearch (live)');
      } else {
        data = await tryStaticData();
        if (data) {
          setStatus('ok', 'Loaded from static data' + (data.generated_at ? ' (' + new Date(data.generated_at).toLocaleString() + ')' : ''));
        } else {
          setStatus('error', 'No data source. Click the gear icon to connect to ES, or deploy with analytics-data.json.');
          document.getElementById('refresh-btn').classList.remove('loading');
          return;
        }
      }
      renderAll(data);
    } catch (err) {
      console.error('Failed to load analytics:', err);
      setStatus('error', 'Error: ' + err.message);
      const data = await tryStaticData();
      if (data) {
        setStatus('ok', 'Fell back to static data (ES unavailable)');
        renderAll(data);
      }
    } finally {
      document.getElementById('refresh-btn').classList.remove('loading');
    }
  }

  function destroyCharts() {
    Object.values(charts).forEach(c => c.destroy());
    Object.keys(charts).forEach(k => delete charts[k]);
  }

  function initModal() {
    const modal = document.getElementById('config-modal');
    const cfg = loadConfig();
    document.getElementById('es-url').value     = cfg.url   || '';
    document.getElementById('es-user').value    = cfg.user  || '';
    document.getElementById('es-pass').value    = cfg.pass  || '';
    document.getElementById('es-index').value   = cfg.index || CONFIG.defaultIndex;
    document.getElementById('cost-input').value  = cfg.costInput  || CONFIG.defaultCostInput;
    document.getElementById('cost-output').value = cfg.costOutput || CONFIG.defaultCostOutput;
    document.getElementById('cfg-min-analysis').value  = cfg.minAnalysis  || CONFIG.minutesSavedPerAnalysis;
    document.getElementById('cfg-min-pr').value        = cfg.minPR        || CONFIG.minutesSavedPerPRReview;
    document.getElementById('cfg-hourly-cost').value   = cfg.hourlyCost   || CONFIG.hourlyEngineerCost;

    document.getElementById('config-btn').addEventListener('click', () => { modal.hidden = false; });
    document.getElementById('config-close').addEventListener('click', () => { modal.hidden = true; });
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.hidden = true; });

    document.getElementById('config-save').addEventListener('click', () => {
      const c = {
        url:   document.getElementById('es-url').value.trim(),
        user:  document.getElementById('es-user').value.trim(),
        pass:  document.getElementById('es-pass').value,
        index: document.getElementById('es-index').value.trim() || CONFIG.defaultIndex,
        costInput:   document.getElementById('cost-input').value,
        costOutput:  document.getElementById('cost-output').value,
        minAnalysis: document.getElementById('cfg-min-analysis').value,
        minPR:       document.getElementById('cfg-min-pr').value,
        hourlyCost:  document.getElementById('cfg-hourly-cost').value,
      };
      CONFIG.minutesSavedPerAnalysis = parseInt(c.minAnalysis) || CONFIG.minutesSavedPerAnalysis;
      CONFIG.minutesSavedPerPRReview = parseInt(c.minPR)       || CONFIG.minutesSavedPerPRReview;
      CONFIG.hourlyEngineerCost      = parseInt(c.hourlyCost)  || CONFIG.hourlyEngineerCost;
      saveConfig(c);
      modal.hidden = true;
      destroyCharts();
      loadData();
    });

    document.getElementById('config-clear').addEventListener('click', () => {
      localStorage.removeItem(CONFIG.storageKey);
      document.getElementById('es-url').value   = '';
      document.getElementById('es-user').value  = '';
      document.getElementById('es-pass').value  = '';
      document.getElementById('es-index').value = CONFIG.defaultIndex;
    });

    if (cfg.minAnalysis) CONFIG.minutesSavedPerAnalysis = parseInt(cfg.minAnalysis) || CONFIG.minutesSavedPerAnalysis;
    if (cfg.minPR)       CONFIG.minutesSavedPerPRReview = parseInt(cfg.minPR)       || CONFIG.minutesSavedPerPRReview;
    if (cfg.hourlyCost)  CONFIG.hourlyEngineerCost      = parseInt(cfg.hourlyCost)  || CONFIG.hourlyEngineerCost;
  }

  function init() {
    Chart.defaults.font.family = T.fontFamily;
    initModal();
    document.getElementById('refresh-btn').addEventListener('click', loadData);

    const rangeSel  = document.getElementById('time-range');
    const dateRange = document.getElementById('date-range');
    const dateFrom  = document.getElementById('date-from');
    const dateTo    = document.getElementById('date-to');
    const today     = new Date().toISOString().slice(0, 10);
    dateTo.value    = today;
    dateTo.max      = today;
    dateFrom.value  = new Date(Date.now() - 6048e5).toISOString().slice(0, 10);
    dateFrom.max    = today;

    rangeSel.addEventListener('change', () => {
      dateRange.hidden = rangeSel.value !== 'custom';
      if (rangeSel.value !== 'custom') { destroyCharts(); loadData(); }
    });
    document.getElementById('date-apply').addEventListener('click', () => { destroyCharts(); loadData(); });

    loadData();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
