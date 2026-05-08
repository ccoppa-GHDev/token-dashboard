import { api, fmt } from '/web/app.js';

export default async function (root) {
  const resp = await api('/api/projects');
  const rows = resp.rows || [];
  const meta = resp._meta || {};
  const costLabel = fmt.planCostLabel(meta);
  const subtitle = meta.is_subscription
    ? `Sorted by billable token spend. "${costLabel}" shows each project's allocated slice of your ${fmt.htmlSafe(meta.plan_label)} fee ($${Number(meta.monthly_fee).toFixed(0)}/mo × ${meta.months_in_range} mo = $${Number(meta.total_paid_usd).toFixed(2)} in this range), weighted by API-equivalent cost.`
    : 'Sorted by billable token spend. Cache reads are billed cheaper, so high cache-read columns are good.';
  root.innerHTML = `
    <div class="card">
      <h2>Projects</h2>
      <p class="muted" style="margin:-8px 0 14px">${subtitle}</p>
      <table>
        <thead><tr><th>project</th><th class="num">sessions</th><th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th><th class="num">${costLabel}</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.turns)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td class="num">${fmt.planCostCell(r)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}
