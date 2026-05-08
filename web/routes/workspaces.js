import { api, fmt } from '/web/app.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
];

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/workspaces';
  location.hash = '#' + base + '?range=' + encodeURIComponent(key);
}

function isoBack(days) {
  return new Date(Date.now() - days * 86400 * 1000).toISOString();
}

function fmtSyncTs(ts) {
  if (ts == null) return 'never synced';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

async function refresh(range) {
  const body = { starting_at: isoBack(range.days), ending_at: new Date().toISOString() };
  const r = await fetch('/api/workspaces/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `refresh failed (${r.status})`);
  return data;
}

export default async function (root) {
  const range = readRange();
  const since = isoBack(range.days);
  const url = '/api/workspaces?since=' + encodeURIComponent(since);
  const data = await api(url);
  const rows = data.rows || [];
  const meta = data._meta || {};

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  const cacheWrite = w => (w.cache_create_5m_tokens || 0) + (w.cache_create_1h_tokens || 0);
  // Console aggregates "Total tokens in" = uncached input + cache reads + cache writes.
  // Match that here so this number is directly comparable to console.anthropic.com.
  const tokensIn = w => (w.input_tokens || 0) + (w.cache_read_tokens || 0) + cacheWrite(w);

  const totalCost = rows.reduce((s, r) => s + (r.cost_usd || 0), 0);
  const totalTokensIn = rows.reduce((s, r) => s + tokensIn(r), 0);
  const totalOutput = rows.reduce((s, r) => s + (r.output_tokens || 0), 0);

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Workspaces</h2>
      <span class="muted" style="font-size:12px">last ${range.days} days</span>
      <div class="spacer"></div>
      ${rangeTabs}
      <button id="ws-refresh" class="primary" style="margin-left:8px">Refresh from Anthropic</button>
    </div>

    <p class="muted" style="margin:-4px 0 14px;font-size:12px">
      Workspace data from Anthropic's Admin API. Cost is computed from <code>pricing.json</code> (input + output + cache reads + cache writes per model), matching the math the rest of the dashboard uses for local JSONL data.
      Pro Max subscription usage isn't reflected here.
      <span style="margin-left:8px">Last synced: <span class="mono">${fmt.htmlSafe(fmtSyncTs(meta.last_synced_at))}</span></span>
    </p>

    <div id="ws-error" class="card" style="display:none;margin-bottom:16px;border-color:#A8324A"></div>

    <div class="row cols-3">
      <div class="card kpi"><div class="label">Workspaces</div><div class="value">${fmt.int(rows.length)}</div></div>
      <div class="card kpi"><div class="label">Total cost (range)</div><div class="value">${fmt.usd(totalCost)}</div></div>
      <div class="card kpi"><div class="label">Tokens in / out (Console-equiv)</div><div class="value" style="font-size:14px">${fmt.compact(totalTokensIn)} / ${fmt.compact(totalOutput)}</div></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>By workspace</h3>
      <table>
        <thead><tr>
          <th>name</th>
          <th>type</th>
          <th class="num" title="Uncached input + cache reads + cache writes — comparable to Anthropic Console's &quot;Total tokens in&quot;">tokens in</th>
          <th class="num">output</th>
          <th class="num" title="Uncached input only">input</th>
          <th class="num">cache read</th>
          <th class="num" title="Cache create (5m + 1h TTL)">cache write</th>
          <th class="num">cost</th>
          <th>last activity</th>
        </tr></thead>
        <tbody>
          ${rows.map(w => `
            <tr>
              <td>${fmt.htmlSafe(w.name || w.workspace_id)}</td>
              <td><span class="badge">${fmt.htmlSafe(w.type || '')}</span></td>
              <td class="num">${fmt.int(tokensIn(w))}</td>
              <td class="num">${fmt.int(w.output_tokens)}</td>
              <td class="num">${fmt.int(w.input_tokens)}</td>
              <td class="num">${fmt.int(w.cache_read_tokens)}</td>
              <td class="num">${fmt.int(cacheWrite(w))}</td>
              <td class="num">${fmt.usd(w.cost_usd)}</td>
              <td class="mono">${fmt.ts(w.last_activity)}</td>
            </tr>`).join('') || `<tr><td colspan="9" class="muted">No workspaces synced yet. Click <strong>Refresh from Anthropic</strong> above.</td></tr>`}
        </tbody>
      </table>
    </div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });

  const btn = document.getElementById('ws-refresh');
  const errBox = document.getElementById('ws-error');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    errBox.style.display = 'none';
    try {
      await refresh(range);
      // Re-render to pull the new rows.
      const fresh = await api(url);
      const main = document.getElementById('app');
      // Simplest re-render: re-invoke this module's default export.
      const mod = await import('/web/routes/workspaces.js?t=' + Date.now());
      main.innerHTML = '';
      await mod.default(main);
    } catch (e) {
      errBox.style.display = '';
      errBox.innerHTML = `<strong>Refresh failed.</strong> <span class="mono">${fmt.htmlSafe(String(e.message || e))}</span>`;
      btn.disabled = false;
      btn.textContent = 'Refresh from Anthropic';
    }
  });
}
