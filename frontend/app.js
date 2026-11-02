const content = document.querySelector('#content');
const crumb = document.querySelector('#crumb');
const healthLabel = document.querySelector('#health-label');
const navItems = [...document.querySelectorAll('.nav-item')];

const viewTitles = {
  overview: 'Overview',
  dies: 'Die results',
  memories: 'Memory inventory',
  failures: 'Fail events',
  analysis: 'Test analysis',
};

const palette = ['#45d6d1', '#7395ff', '#ad8bff', '#ffb86b', '#fb7185', '#63d9a0', '#7bb7f4'];
const state = { view: 'overview', offset: 0, limit: 25, filters: {}, notice: '' };
const dieFormFields = [
  ['lot_id', 'Lot ID', 'text'],
  ['wafer_id', 'Wafer ID', 'text'],
  ['die_x', 'Die X', 'integer'],
  ['die_y', 'Die Y', 'integer'],
  ['radial_pos', 'Radial position (0–1)', 'float', 0, 1],
  ['ro_speed_mhz', 'RO speed (MHz)', 'float', 0],
  ['leakage_na', 'Leakage (nA)', 'float', 0],
  ['iddq_ua', 'IDDQ (µA)', 'float', 0],
  ['bitcell_vmin_mv', 'Bitcell Vmin (mV)', 'float', 0],
  ['scan_pass', 'Scan passed', 'boolean'],
  ['march_failing_mems', 'March failing memories', 'integer', 0],
  ['early_mats_fails', 'Early MATS+ failures', 'integer', 0],
  ['repaired_rows', 'Repaired rows', 'integer', 0],
  ['repaired_cols', 'Repaired columns', 'integer', 0],
  ['retention_fail', 'Retention failure', 'boolean'],
  ['dynamic_fault_fail', 'Dynamic fault failure', 'boolean'],
  ['vmin_corner_only_fail', 'Vmin-corner-only failure', 'boolean'],
  ['unrepairable', 'Unrepairable', 'boolean'],
  ['neighbor_fail_cnt', 'Neighbor fail count', 'integer', 0],
  ['final_bin', 'Final bin', 'bin'],
  ['t_march_ms', 'March time (ms)', 'float', 0],
  ['t_retention_ms', 'Retention time (ms)', 'float', 0],
  ['t_repair_ms', 'Repair time (ms)', 'float', 0],
  ['t_setup_readout_ms', 'Setup/readout time (ms)', 'float', 0],
  ['t_total_baseline_ms', 'Total baseline time (ms)', 'float', 0],
];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function formatNumber(value, digits = 0) {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(value);
}

function formatBits(value) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} Gbit`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} Mbit`;
  return `${formatNumber(value)} bit`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function pageHeader(title, subtitle, eyebrow = 'MBIST TEST INTELLIGENCE') {
  return `<div class="heading-row">
    <div><div class="eyebrow">${escapeHtml(eyebrow)}</div>
      <h1 class="page-heading">${escapeHtml(title)}</h1>
      <p class="page-subtitle">${escapeHtml(subtitle)}</p></div>
  </div>`;
}

function kpi(label, value, note, color) {
  return `<article class="kpi" style="--accent:${color}">
    <div class="kpi-label">${escapeHtml(label)}</div>
    <div class="kpi-value">${escapeHtml(value)}</div>
    <div class="kpi-note">${escapeHtml(note)}</div>
  </article>`;
}

function panel(title, hint, body, tag = '') {
  return `<section class="panel">
    <div class="panel-head"><div><h2 class="panel-title">${escapeHtml(title)}</h2>
      <div class="panel-hint">${escapeHtml(hint)}</div></div>
      ${tag ? `<span class="panel-tag">${escapeHtml(tag)}</span>` : ''}</div>
    ${body}
  </section>`;
}

function bars(items, color = '#45d6d1') {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (!items.length) return '<div class="empty-state">No records found.</div>';
  return `<div class="bar-chart">${items.map((item, index) => `
    <div class="bar-row">
      <div class="bar-label" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(1, item.count / max * 100)}%;--bar-color:${palette[index % palette.length] || color}"></div></div>
      <div class="bar-value">${formatNumber(item.count)}</div>
    </div>`).join('')}</div>`;
}

function lotChart(items) {
  const max = Math.max(1, ...items.map((item) => item.dies));
  return `<div class="lot-chart">${items.map((lot) => {
    const height = lot.dies / max * 100;
    const failHeight = lot.fails / max * 100;
    return `<div class="lot-column" title="${escapeHtml(lot.name)}: ${formatNumber(lot.fails)} failing of ${formatNumber(lot.dies)} die">
      <div class="lot-stack" style="height:${height}%"><div class="lot-pass" style="height:${Math.max(0, height - failHeight) / height * 100}%"></div><div class="lot-fail" style="height:${height ? failHeight / height * 100 : 0}%"></div></div>
      <span class="lot-label">${escapeHtml(lot.name.replace('LOT', 'L'))}</span>
    </div>`;
  }).join('')}</div>`;
}

async function renderOverview() {
  content.innerHTML = '<div class="loading-panel"><span class="spinner"></span>Loading overview…</div>';
  const summary = await api('/api/summary');
  const t = summary.totals;
  content.innerHTML = `
    ${pageHeader('Silicon test, in focus.', 'Explore synthetic wafer-sort outcomes, memory inventory, and fail signatures. Use the analysis view to compare algorithm coverage and power-capped schedules.', 'DATASET OVERVIEW')}
    <div class="kpi-grid">
      ${kpi('Die records', formatNumber(t.dies), `${formatNumber(t.lots)} lots · ${t.scan_pass_pct}% scan pass`, '#45d6d1')}
      ${kpi('Memory instances', formatNumber(t.memories), `${formatNumber(t.controllers)} MBIST controllers`, '#7395ff')}
      ${kpi('Fail events', formatNumber(t.fail_events), 'Failing-memory observations', '#ad8bff')}
      ${kpi('Baseline test time', `${t.baseline_ms} ms`, `${formatBits(t.total_bits)} across ${formatNumber(t.memories)} memories`, '#ffb86b')}
    </div>
    <div class="two-col">
      ${panel('Final test bin mix', 'Disposition across all die in the dataset', bars(summary.bins), 'DIE OUTCOMES')}
      ${panel('Lot-level yield profile', 'Die count by lot; failing bins highlighted', `${lotChart(summary.lots)}<div class="legend" style="margin-top:22px"><span><i></i>Non-fail bin</span><span><i class="legend-fail"></i>Fail bin</span></div>`, `${formatNumber(t.lots)} LOTS`)}
    </div>
    <div class="two-col">
      ${panel('Observed fault signatures', 'Fail-event frequency by fault type', bars(summary.fault_types, '#ad8bff'), 'EVENTS')}
      ${panel('Memory portfolio', 'Instances grouped by memory type', bars(summary.memory_types, '#7395ff'), `${formatNumber(t.test_power_mw)} mW TOTAL POWER`)}
    </div>
    <div class="callout"><strong>Synthetic-data notice.</strong> These generated measurements are for method development only. Adaptive-flow metrics shown in the supplied validation report are reference results and are not a production recommendation.</div>`;
}

function setView(view) {
  if (!viewTitles[view]) return;
  state.view = view;
  state.offset = 0;
  state.filters = {};
  navItems.forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  crumb.textContent = viewTitles[view];
  if (view === 'overview') renderOverview().catch(showError);
  else if (view === 'analysis') renderAnalysis().catch(showError);
  else renderTable().catch(showError);
}

function tableConfig() {
  const configs = {
    dies: {
      endpoint: '/api/dies', heading: 'Die results', eyebrow: 'WAFER SORT · DIE-LEVEL DATA',
      subtitle: 'Browse parametrics, MBIST outcomes, and baseline test timing for each die.',
      download: 'dies',
      filters: [
        { key: 'lot', label: 'All lots', options: [] },
        { key: 'bin', label: 'All final bins', options: [] },
      ],
      search: 'Search die or wafer ID…',
    },
    memories: {
      endpoint: '/api/memories', heading: 'Memory inventory', eyebrow: 'SOC INVENTORY · MEMORY-LEVEL DATA',
      subtitle: 'Inspect memory geometry, repair resources, controller assignment, and test power.',
      download: 'memories',
      filters: [
        { key: 'mem_type', label: 'All memory types', options: ['SRAM_SP', 'SRAM_DP', 'RF_2P', 'ROM'] },
        { key: 'bitcell', label: 'All bitcell classes', options: ['HD', 'HC', 'ROM'] },
      ],
      search: 'Search memory or controller ID…',
    },
    failures: {
      endpoint: '/api/failures', heading: 'Fail events', eyebrow: 'FAIL SIGNATURES · EVENT-LEVEL DATA',
      subtitle: 'Review failing memory events, fault class, detection background, and algorithm coverage.',
      download: 'failures',
      filters: [
        { key: 'fault_type', label: 'All fault types', options: ['SAF', 'TF', 'ADF', 'CFid', 'CFin', 'RDF', 'DRDF', 'WDF'] },
        { key: 'bitcell', label: 'All bitcell classes', options: ['HD', 'HC'] },
      ],
      search: 'Search die or memory ID…',
    },
  };
  return configs[state.view];
}

function filterMarkup(config, key, label, options) {
  const selectOptions = options.map((option) =>
    `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join('');
  return `<select class="control" data-filter="${escapeHtml(key)}" aria-label="${escapeHtml(label)}">
    <option value="">${escapeHtml(label)}</option>${selectOptions}</select>`;
}

function renderCell(field, value) {
  if (value === null || value === undefined) return '<span class="muted">—</span>';
  if (field === 'final_bin') {
    const failing = String(value).startsWith('FAIL');
    return `<span class="badge ${failing ? 'fail' : 'pass'}">${escapeHtml(value)}</span>`;
  }
  if (typeof value === 'boolean') return `<span class="badge ${value ? 'pass' : ''}">${value ? 'Yes' : 'No'}</span>`;
  if (typeof value === 'number') return escapeHtml(Number.isInteger(value) ? formatNumber(value) : formatNumber(value, 2));
  return escapeHtml(value);
}

function dieFieldMarkup([name, label, type, min, max]) {
  let input;
  if (type === 'boolean') {
    input = `<select class="control" name="${name}" required><option value="">Choose…</option><option value="true">Yes</option><option value="false">No</option></select>`;
  } else if (type === 'bin') {
    input = `<select class="control" name="${name}" required><option value="">Choose a bin…</option>${['PASS', 'PASS_REPAIRED', 'FAIL_SCAN', 'FAIL_UNREPAIRABLE', 'FAIL_RETENTION', 'FAIL_DYNAMIC', 'FAIL_VMIN_CORNER'].map((value) => `<option value="${value}">${value}</option>`).join('')}</select>`;
  } else if (type === 'text') {
    input = `<input class="control" name="${name}" type="text" maxlength="80" required>`;
  } else {
    input = `<input class="control" name="${name}" type="number" ${type === 'integer' ? 'step="1"' : 'step="any"'} ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''} required>`;
  }
  return `<label class="die-field"><span>${escapeHtml(label)}</span>${input}</label>`;
}

function addDiePanelMarkup(adminSession) {
  let accessPanel;
  if (!adminSession.configured) {
    accessPanel = '<div class="callout">Adding records is not configured yet. The owner must set <code>DIE_ADMIN_PASSWORD</code> and <code>DATABASE_URL</code> in the Render service environment.</div>';
  } else if (!adminSession.authenticated) {
    accessPanel = `<form data-admin-login class="admin-login">
      <label class="die-field"><span>Admin password</span><input class="control" name="password" type="password" autocomplete="current-password" required></label>
      <button class="button button-quiet" type="submit">Sign in</button>
      <p class="form-message" data-form-message aria-live="polite"></p>
    </form>`;
  } else {
    accessPanel = `<div class="add-die-toolbar"><span class="panel-hint">Signed in · submissions are saved to PostgreSQL.</span><button class="button button-quiet" type="button" data-admin-logout>Sign out</button></div>
      <details class="die-form-details"><summary>Add a die record</summary>
        <p class="panel-hint">The die ID is generated automatically. Enter the complete record; total baseline time must equal the four time components.</p>
        <form data-add-die-form>
          <div class="die-form-grid">${dieFormFields.map(dieFieldMarkup).join('')}</div>
          <button class="button button-quiet" type="submit">Save die record</button>
          <p class="form-message" data-form-message aria-live="polite">${escapeHtml(state.notice)}</p>
        </form>
      </details>`;
  }
  return `<section class="panel add-die-panel">
    <div class="panel-head"><div><h2 class="panel-title">Add a die record</h2><div class="panel-hint">Protected admin entry · new records appear in die results and CSV exports.</div></div></div>
    ${accessPanel}
  </section>`;
}

async function renderTable() {
  const config = tableConfig();
  const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
  Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  if (state.filters.search) params.set('search', state.filters.search);
  content.innerHTML = '<div class="loading-panel"><span class="spinner"></span>Loading records…</div>';
  const [result, summary, adminSession] = await Promise.all([
    api(`${config.endpoint}?${params}`),
    state.view === 'dies' ? api('/api/summary') : Promise.resolve(null),
    state.view === 'dies' ? api('/api/admin/session') : Promise.resolve(null),
  ]);
  const filterControls = config.filters.map((filter) => {
    const options = filter.options.length
      ? filter.options
      : filter.key === 'lot'
        ? summary.lots.map((item) => item.name)
        : summary.bins.map((item) => item.name);
    return filterMarkup(config, filter.key, filter.label, options);
  }).join('');
  const searchValue = state.filters.search || '';
  const table = result.items.length
    ? `<div class="table-wrap"><table class="data-table"><thead><tr>${result.fields.map((field) => `<th>${escapeHtml(field.replaceAll('_', ' '))}</th>`).join('')}</tr></thead>
      <tbody>${result.items.map((row) => `<tr>${result.fields.map((field) => `<td>${renderCell(field, row[field])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : '<div class="empty-state">No records match these filters.</div>';
  const first = result.total ? result.offset + 1 : 0;
  const last = Math.min(result.offset + result.limit, result.total);
  content.innerHTML = `
    ${pageHeader(config.heading, config.subtitle, config.eyebrow)}
    ${state.view === 'dies' ? addDiePanelMarkup(adminSession) : ''}
    <div class="panel">
      <div class="panel-head"><div><h2 class="panel-title">${formatNumber(result.total)} records</h2><div class="panel-hint">Filter, inspect, and export the original CSV dataset.</div></div>
        <a class="button button-quiet" href="/api/download/${config.download}">Download CSV</a></div>
      <div class="filter-row">${filterControls}<input class="control search" data-search type="search" value="${escapeHtml(searchValue)}" placeholder="${escapeHtml(config.search)}" aria-label="${escapeHtml(config.search)}"></div>
      ${table}
      <div class="pager"><span>Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(result.total)}</span>
        <div class="pager-actions"><button data-page="prev" ${state.offset === 0 ? 'disabled' : ''}>Previous</button>
        <button data-page="next" ${state.offset + state.limit >= result.total ? 'disabled' : ''}>Next</button></div></div>
    </div>`;

  content.querySelectorAll('[data-filter]').forEach((select) => {
    select.value = state.filters[select.dataset.filter] || '';
    select.addEventListener('change', () => {
      state.filters[select.dataset.filter] = select.value;
      state.offset = 0;
      renderTable().catch(showError);
    });
  });
  const search = content.querySelector('[data-search]');
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      state.filters.search = search.value.trim();
      state.offset = 0;
      renderTable().catch(showError);
    }
  });
  content.querySelectorAll('[data-page]').forEach((button) => {
    button.addEventListener('click', () => {
      state.offset += button.dataset.page === 'next' ? state.limit : -state.limit;
      renderTable().catch(showError);
    });
  });
  const loginForm = content.querySelector('[data-admin-login]');
  if (loginForm) {
    loginForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = loginForm.querySelector('[data-form-message]');
      const button = loginForm.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        await api('/api/admin/session', {
          method: 'POST',
          body: JSON.stringify({ password: new FormData(loginForm).get('password') }),
        });
        state.notice = '';
        renderTable().catch(showError);
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }
  const logoutButton = content.querySelector('[data-admin-logout]');
  if (logoutButton) {
    logoutButton.addEventListener('click', async () => {
      try {
        await api('/api/admin/session', { method: 'DELETE' });
        state.notice = '';
        renderTable().catch(showError);
      } catch (error) {
        showError(error);
      }
    });
  }
  const addDieForm = content.querySelector('[data-add-die-form]');
  if (addDieForm) {
    addDieForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = addDieForm.querySelector('[data-form-message]');
      const button = addDieForm.querySelector('button[type="submit"]');
      const payload = Object.fromEntries(new FormData(addDieForm).entries());
      dieFormFields.forEach(([name, , type]) => {
        if (type === 'boolean') payload[name] = payload[name] === 'true';
        else if (type === 'integer' || type === 'float') payload[name] = Number(payload[name]);
      });
      button.disabled = true;
      message.textContent = 'Saving record…';
      try {
        const result = await api('/api/dies', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        state.notice = `Saved ${result.item.die_id}.`;
        state.offset = 0;
        renderTable().catch(showError);
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }
}

function renderAlgorithmCard(group) {
  const maxOperations = 22;
  return `<article class="algorithm-card">
    <div class="algorithm-top"><strong>${escapeHtml(group.bitcell)} bitcell</strong><span>${formatNumber(group.events)} observed fail events</span></div>
    ${group.coverage.map((row, index) => `<div class="algorithm-line">
      <span class="algorithm-name">${escapeHtml(row.name)} · ${row.operations_per_address}N</span>
      <div class="bar-track"><div class="bar-fill" style="width:${row.coverage_pct}%;--bar-color:${index === 4 ? '#63d9a0' : palette[index]}"></div></div>
      <span class="algorithm-coverage">${row.coverage_pct.toFixed(2)}%</span>
    </div>`).join('')}
    <div class="recommendation">${group.recommended
      ? `First candidate meeting the supplied 0.5% escape-bound criterion: <strong>${escapeHtml(group.recommended)}</strong>`
      : 'No candidate meets the supplied escape-bound criterion.'}</div>
  </article>`;
}

async function renderAnalysis() {
  content.innerHTML = '<div class="loading-panel"><span class="spinner"></span>Calculating analysis…</div>';
  const analysis = await api('/api/analysis');
  const schedule = analysis.scheduling;
  const adaptive = analysis.adaptive_reference;
  const adaptiveSweep = `<div class="table-wrap" style="margin-top:14px"><table class="data-table">
    <thead><tr><th>Low tier</th><th>Mean ms</th><th>Time saved</th><th>Expected escapes</th><th>Added DPPM</th></tr></thead>
    <tbody>${adaptive.sweep.map((row) => `<tr><td>${row.low_tier_pct}%</td><td>${row.mean_ms}</td><td>${row.saving_pct}%</td><td>${row.expected_escapes}</td><td>${row.added_dppm}</td></tr>`).join('')}</tbody>
    </table></div>`;
  content.innerHTML = `
    ${pageHeader('Coverage vs. test time.', 'Compare observed algorithm detection on the supplied fail events and a first-fit power-capped schedule. These are dataset analyses, not a sign-off for silicon.', 'METHOD VALIDATION')}
    <div class="callout"><strong>Interpretation:</strong> Algorithm recommendations use the same Agresti–Coull-style upper escape bound and 0.5% threshold as the supplied validation script. Fail-event coverage is observed synthetic-data coverage, not a guarantee of field quality.</div>
    ${panel('Adaptive flow · reference run', `Precomputed from ${adaptive.source}; this dashboard does not retrain the model.`,
      `<div class="schedule-compare">
        <div class="schedule-box"><small>HELD-OUT LOT AUC</small><strong>${adaptive.held_out_auc.toFixed(3)}</strong></div>
        <div class="schedule-box"><small>TEST PREVALENCE</small><strong>${adaptive.target_prevalence_pct}%</strong></div>
        <div class="schedule-box"><small>LOW TIER WITHIN ${adaptive.dppm_budget} DPPM</small><strong>${adaptive.best_low_tier_pct}% of die</strong></div>
        <div class="schedule-box"><small>TIME SAVED · ${adaptive.best_added_dppm} DPPM</small><strong>${adaptive.best_saving_pct}%</strong></div>
      </div>${adaptiveSweep}`,
      'PRECOMPUTED VALIDATION')}
    <div class="analysis-grid" style="margin-top:14px">
      ${panel('Algorithm coverage by bitcell', 'Observed detection rate across candidate March algorithms', analysis.algorithm_pruning.map(renderAlgorithmCard).join(''), 'FAIL-EVENT DATA')}
      ${panel('Power-capped scheduling', 'One memory per controller per session; 8% of total memory test power',
        `<div class="schedule-metric"><strong>${schedule.improvement_pct}%</strong><span>shorter total March session time</span></div>
        <div class="bar-row"><div class="bar-label">Inventory order</div><div class="bar-track"><div class="bar-fill" style="width:100%;--bar-color:#ffb86b"></div></div><div class="bar-value">${schedule.inventory_order.total_ms} ms</div></div>
        <div class="bar-row" style="margin-top:11px"><div class="bar-label">Longest first</div><div class="bar-track"><div class="bar-fill" style="width:${schedule.inventory_order.total_ms ? schedule.longest_first.total_ms / schedule.inventory_order.total_ms * 100 : 0}%;--bar-color:#45d6d1"></div></div><div class="bar-value">${schedule.longest_first.total_ms} ms</div></div>
        <div class="schedule-compare">
          <div class="schedule-box"><small>INVENTORY ORDER</small><strong>${schedule.inventory_order.sessions} sessions</strong></div>
          <div class="schedule-box"><small>LONGEST-FIRST</small><strong>${schedule.longest_first.sessions} sessions</strong></div>
        </div>
        <p class="panel-hint" style="margin-top:14px">Power cap: ${formatNumber(schedule.power_cap_mw)} mW. Scheduling uses first-fit placement; it is a heuristic, not a global optimum.</p>`,
        'MEMORY INVENTORY')}
    </div>`;
}

function showError(error) {
  console.error(error);
  content.innerHTML = `<div class="error-panel"><strong>Unable to load this view.</strong><br>${escapeHtml(error.message)}<br><button class="button button-quiet" style="margin-top:13px" data-retry>Retry</button></div>`;
  content.querySelector('[data-retry]').addEventListener('click', () => setView(state.view));
}

navItems.forEach((item) => item.addEventListener('click', () => setView(item.dataset.view)));
api('/api/health').then((health) => {
  healthLabel.textContent = 'Data connected';
  healthLabel.title = Object.entries(health.rows).map(([name, count]) => `${name}: ${formatNumber(count)}`).join(' · ');
}).catch((error) => {
  healthLabel.textContent = 'Data error';
  healthLabel.title = error.message;
});
renderOverview().catch(showError);
