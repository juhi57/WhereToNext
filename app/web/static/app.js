/* WhereToNext front end.
 *
 * Vanilla ES2020, no bundler, no framework - the repository should run with a
 * single uvicorn command. The server is the source of truth for every
 * enumeration, so the form is built from /api/options rather than duplicating
 * the vocabularies here and letting them drift. */

'use strict';

const API = '/api';

const CURRENCY_FORMATS = {
  INR: { locale: 'en-IN', digits: 0 },
  JPY: { locale: 'ja-JP', digits: 0 },
};

const VERDICT = {
  under_budget: { label: 'Under budget', cls: 'badge-good' },
  on_budget:    { label: 'Within budget', cls: 'badge-good' },
  near_limit:   { label: 'At the limit', cls: 'badge-warn' },
  over_budget:  { label: 'Over budget', cls: 'badge-bad' },
};

const el = (id) => document.getElementById(id);
const form = el('trip-form');

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

/** Escape text before it goes anywhere near innerHTML. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function money(amount, currency) {
  const fmt = CURRENCY_FORMATS[currency] || { locale: 'en-US', digits: 2 };
  try {
    return new Intl.NumberFormat(fmt.locale, {
      style: 'currency',
      currency,
      minimumFractionDigits: fmt.digits,
      maximumFractionDigits: fmt.digits,
    }).format(amount || 0);
  } catch {
    return `${currency} ${Math.round(amount || 0).toLocaleString()}`;
  }
}

function titleCase(value) {
  return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function duration(minutes) {
  if (!minutes) return '';
  const h = Math.floor(minutes / 60), m = minutes % 60;
  if (h && m) return `${h}h ${m}m`;
  return h ? `${h}h` : `${m}m`;
}

function longDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
}

function show(node, visible) { node.hidden = !visible; }

/** Read the form into the JSON body the API expects. */
function readForm() {
  const data = new FormData(form);
  const number = (key) => {
    const raw = data.get(key);
    return raw === '' || raw === null ? null : Number(raw);
  };
  const text = (key) => {
    const raw = (data.get(key) || '').toString().trim();
    return raw === '' ? null : raw;
  };
  return {
    origin: text('origin') ?? '',
    destination: text('destination') ?? '',
    start_date: data.get('start_date'),
    end_date: data.get('end_date'),
    adults: number('adults') ?? 1,
    children: number('children') ?? 0,
    budget_total: number('budget_total') ?? 0,
    currency: data.get('currency'),
    transport_modes: [data.get('transport_modes')],
    stay_type: data.get('stay_type'),
    food_preference: data.get('food_preference'),
    pace: data.get('pace'),
    interests: [...form.querySelectorAll('#interests input:checked')].map((i) => i.value),
    accessibility_needs: text('accessibility_needs'),
    notes: text('notes'),
  };
}

function showErrors(messages) {
  const box = el('form-errors');
  if (!messages.length) { show(box, false); return; }
  box.innerHTML = `<strong>Please fix the following:</strong><ul>${
    messages.map((m) => `<li>${esc(m)}</li>`).join('')
  }</ul>`;
  show(box, true);
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** POST JSON and turn the API's error envelope into a thrown Error. */
async function postJson(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const err = payload && payload.error;
    if (err && Array.isArray(err.fields) && err.fields.length) {
      throw new Error(err.fields.map((f) => `${titleCase(f.field)}: ${f.message}`).join('\n'));
    }
    throw new Error((err && err.message) || `Request failed (${response.status})`);
  }
  return payload;
}

/* ------------------------------------------------------------------ */
/* form construction                                                   */
/* ------------------------------------------------------------------ */

function fillSelect(id, items, selected) {
  el(id).innerHTML = items
    .map((o) => `<option value="${esc(o.value)}"${o.value === selected ? ' selected' : ''}>${esc(o.label)}</option>`)
    .join('');
}

function defaultDates() {
  const out = new Date(); out.setDate(out.getDate() + 45);
  const back = new Date(out); back.setDate(back.getDate() + 5);
  const iso = (d) => d.toISOString().slice(0, 10);
  const today = iso(new Date());
  for (const name of ['start_date', 'end_date']) form[name].min = today;
  form.start_date.value = iso(out);
  form.end_date.value = iso(back);
}

async function bootstrap() {
  defaultDates();
  try {
    const [opts, health] = await Promise.all([
      fetch(`${API}/options`).then((r) => r.json()),
      fetch(`${API}/health`).then((r) => r.json()),
    ]);

    fillSelect('transport_modes', opts.transport_modes, 'flight');
    fillSelect('stay_type', opts.stay_types, 'mid_hotel');
    fillSelect('food_preference', opts.food_preferences, 'no_preference');
    fillSelect('pace', opts.paces, 'balanced');
    fillSelect('currency', opts.currencies, 'INR');

    el('interests').innerHTML = opts.interests
      .map((i) => `<label class="chip"><input type="checkbox" value="${esc(i.value)}">${esc(i.label)}</label>`)
      .join('');

    el('places').innerHTML = opts.destinations
      .map((d) => `<option value="${esc(d)}"></option>`).join('');

    const badge = el('engine-badge');
    badge.textContent = health.ai_enabled
      ? `AI planner: ${health.planner_model}`
      : 'Template planner (no API key)';
    badge.className = `badge ${health.ai_enabled ? '' : 'badge-muted'}`;
    badge.title = health.ai_enabled
      ? 'Itineraries are generated by Claude, then re-costed by the budget engine.'
      : 'Set ANTHROPIC_API_KEY to enable AI planning. Everything else works as normal.';

    form.currency.addEventListener('change', updateBudgetHint);
    updateBudgetHint();
  } catch {
    el('engine-badge').textContent = 'Server unreachable';
  }
}

function updateBudgetHint() {
  el('budget-hint').textContent = `in ${form.currency.value}, whole party`;
}

/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */

function renderEstimate(data) {
  const v = VERDICT[data.verdict] || VERDICT.on_budget;
  const rows = Object.entries(data.lines).map(([k, amount]) => `
    <tr><td>${esc(titleCase(k))}</td><td class="num">${esc(money(amount, data.currency))}</td></tr>`).join('');

  el('estimate').innerHTML = `
    <div class="plan-head">
      <div class="plan-head-row">
        <h3>Budget check</h3>
        <span class="badge ${v.cls}">${esc(v.label)}</span>
      </div>
      <p class="plan-route">${esc(data.price_tier.replace(/_/g, ' '))} destination &middot;
        about ${Math.round(data.distance_km).toLocaleString()} km each way${data.distance_estimated ? ' (estimated)' : ''}
        &middot; ${data.international ? 'international' : 'domestic'}</p>
    </div>
    <dl class="stat-row">
      <div class="stat"><dt>Estimated cost</dt><dd>${esc(money(data.total, data.currency))}</dd></div>
      <div class="stat"><dt>Your budget</dt><dd>${esc(money(data.budget_total, data.currency))}</dd></div>
      <div class="stat"><dt>Budget used</dt><dd class="${data.utilisation_pct > 100 ? 'neg' : 'pos'}">${data.utilisation_pct}%</dd></div>
      <div class="stat"><dt>Per person</dt><dd>${esc(money(data.per_person, data.currency))}</dd></div>
    </dl>
    <div class="section">
      <h4>Where it goes</h4>
      <table class="budget"><tbody>${rows}</tbody></table>
    </div>
    <div class="section">
      <h4>Notes</h4>
      <ul class="advice">${data.advice.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>
    </div>
    <p class="fineprint">This is a reference costing, generated instantly without an AI call.
      Create the itinerary for a full day-by-day plan.</p>`;
}

function renderBudget(budget) {
  const cur = budget.currency;
  const rows = budget.lines.map((line) => `
    <tr>
      <td>${esc(titleCase(line.category))}<br><span class="note">${esc(line.note)}</span></td>
      <td class="num">${esc(money(line.amount, cur))}</td>
      <td class="num">${line.share_pct.toFixed(1)}%</td>
      <td><div class="bar"><i style="width:${Math.min(line.share_pct, 100)}%"></i></div></td>
    </tr>`).join('');

  return `
    <div class="section">
      <h4>Budget breakdown</h4>
      <table class="budget">
        <thead><tr><th>Category</th><th class="num">Amount</th><th class="num">Share</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          <td>Total</td>
          <td class="num">${esc(money(budget.total, cur))}</td>
          <td class="num">${budget.utilisation_pct.toFixed(0)}%</td>
          <td><span class="note">of budget</span></td>
        </tr></tfoot>
      </table>
      ${budget.advice.length ? `<ul class="advice">${budget.advice.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>` : ''}
    </div>`;
}

function renderDay(day, cur, index) {
  const slots = day.activities.map((a) => `
    <div class="slot">
      <div class="slot-time">${esc(a.start_time || '-')}<span class="slot-dur">${esc(duration(a.duration_minutes))}</span></div>
      <div>
        <div class="slot-title">${esc(a.title)}</div>
        ${a.location ? `<div class="slot-meta">${esc(a.location)} &middot; ${esc(titleCase(a.category))}</div>` : ''}
        ${a.description ? `<p class="slot-desc">${esc(a.description)}</p>` : ''}
        ${a.booking_tip ? `<p class="slot-tip">Tip: ${esc(a.booking_tip)}</p>` : ''}
      </div>
      <div class="slot-cost">${a.cost_total ? esc(money(a.cost_total, cur)) : '<span class="free">Free</span>'}</div>
    </div>`).join('');

  const meals = day.meals.length ? `
    <div class="sub"><h5>Meals</h5><ul>
      ${day.meals.map((m) => `<li><strong>${esc(titleCase(m.meal_type))}</strong> &middot; ${esc(m.suggestion)}
        ${m.location ? ` &middot; ${esc(m.location)}` : ''} &middot; ${esc(money(m.cost_total, cur))}
        ${m.dietary_note ? `<br><span class="note">${esc(m.dietary_note)}</span>` : ''}</li>`).join('')}
    </ul></div>` : '';

  const stay = day.stay ? `<li><strong>Stay</strong> &middot; ${esc(day.stay.name)}
      ${day.stay.area ? ` (${esc(day.stay.area)})` : ''} &middot;
      ${day.stay.charged ? esc(money(day.stay.cost_total, cur)) : '<span class="free">no charge - departure day</span>'}
      ${day.stay.why ? `<br><span class="note">${esc(day.stay.why)}</span>` : ''}</li>` : '';

  const local = (day.local_transport_total || day.local_transport_note)
    ? `<li><strong>Local transport</strong> &middot; ${esc(day.local_transport_note)} &middot; ${esc(money(day.local_transport_total, cur))}</li>`
    : '';

  return `
    <details class="day"${index === 0 ? ' open' : ''}>
      <summary>
        <span class="day-n">Day ${day.day_number}</span>
        <span>${esc(day.title)}</span>
        <span class="day-date">${esc(longDate(day.day_date))}</span>
        <span class="day-cost">${esc(money(day.day_total, cur))}</span>
      </summary>
      <div class="day-body">
        ${slots}
        ${meals}
        ${stay || local ? `<div class="sub"><h5>Logistics</h5><ul>${stay}${local}</ul></div>` : ''}
      </div>
    </details>`;
}

function renderLeg(label, leg, cur) {
  return `<li><strong>${esc(label)}</strong> &middot; ${esc(titleCase(leg.mode))} &middot;
    ${esc(leg.from_place)} &rarr; ${esc(leg.to_place)} &middot; ${esc(leg.duration_hint)} &middot;
    <strong>${esc(money(leg.cost_total, cur))}</strong>
    ${leg.booking_tip ? `<br><span class="note">${esc(leg.booking_tip)}</span>` : ''}</li>`;
}

function renderPlan(itinerary) {
  const b = itinerary.budget, cur = b.currency, r = itinerary.request;
  const v = VERDICT[b.verdict] || VERDICT.on_budget;
  const diffClass = b.difference >= 0 ? 'pos' : 'neg';

  const listSection = (title, items, cls = 'pill-list') => items.length
    ? `<div class="section"><h4>${esc(title)}</h4><ul class="${cls}">${items.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></div>`
    : '';

  el('plan').innerHTML = `
    <div class="plan-head">
      <div class="plan-head-row">
        <h3>${esc(itinerary.title)}</h3>
        <span class="badge ${v.cls}">${esc(v.label)}</span>
      </div>
      <p class="plan-route">${esc(r.origin)} &rarr; ${esc(r.destination)} &middot;
        ${esc(longDate(r.start_date))} to ${esc(longDate(r.end_date))} &middot;
        ${r.duration_days} days, ${r.nights} nights &middot; ${r.travellers} traveller(s)</p>
    </div>

    <dl class="stat-row">
      <div class="stat"><dt>Total cost</dt><dd>${esc(money(b.total, cur))}</dd></div>
      <div class="stat"><dt>Your budget</dt><dd>${esc(money(b.budget_total, cur))}</dd></div>
      <div class="stat"><dt>${b.difference >= 0 ? 'Left over' : 'Over by'}</dt>
        <dd class="${diffClass}">${esc(money(Math.abs(b.difference), cur))}</dd></div>
      <div class="stat"><dt>Per person / day</dt><dd>${esc(money(b.per_person_per_day, cur))}</dd></div>
    </dl>

    ${itinerary.destination_overview ? `<p class="prose">${esc(itinerary.destination_overview)}</p>` : ''}
    ${itinerary.best_time_note ? `<p class="prose"><strong>Timing.</strong> ${esc(itinerary.best_time_note)}</p>` : ''}

    ${renderBudget(b)}

    <div class="section">
      <h4>Getting there and back</h4>
      <ul class="advice">
        ${renderLeg('Outbound', itinerary.outbound, cur)}
        ${renderLeg('Return', itinerary.inbound, cur)}
      </ul>
    </div>

    <div class="section">
      <h4>Day by day</h4>
      ${itinerary.days.map((d, i) => renderDay(d, cur, i)).join('')}
    </div>

    ${itinerary.food_scene_note ? `<div class="section"><h4>Eating well here</h4><p class="prose">${esc(itinerary.food_scene_note)}</p></div>` : ''}
    ${listSection('Packing list', itinerary.packing_list)}
    ${listSection('Practical tips', itinerary.travel_tips)}
    ${listSection('Before you book', itinerary.warnings, 'warn-list')}

    <div class="toolbar">
      <a class="btn btn-primary" style="flex:0 0 auto;text-align:center;text-decoration:none;padding:11px 16px"
         href="${API}/itineraries/${encodeURIComponent(itinerary.id)}/pdf">Download PDF</a>
      <a class="btn btn-ghost" href="${API}/itineraries/${encodeURIComponent(itinerary.id)}/pdf?inline=true"
         target="_blank" rel="noopener" style="text-decoration:none;text-align:center">Preview</a>
      <button type="button" class="btn btn-ghost" id="regenerate">Regenerate</button>
    </div>

    <details class="meta">
      <summary>How these numbers were produced
        (${esc(itinerary.meta.mode === 'ai' ? itinerary.meta.model : 'reference templates')})</summary>
      <p class="note">Unit prices were proposed by the planner, then re-costed against reference
        pricing data. Implausible figures were corrected; every correction is listed here.</p>
      ${itinerary.meta.adjustments.length
        ? `<ul>${itinerary.meta.adjustments.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>`
        : '<p class="note">No corrections were needed.</p>'}
      ${itinerary.meta.mode === 'ai'
        ? `<p class="note">Tokens: ${itinerary.meta.input_tokens} in, ${itinerary.meta.output_tokens} out
           &middot; ${(itinerary.meta.duration_ms / 1000).toFixed(1)}s
           &middot; prompt cache ${itinerary.meta.cache_hit ? 'hit' : 'miss'}</p>` : ''}
    </details>`;

  el('regenerate').addEventListener('click', () => submitPlan(true));
}

/* ------------------------------------------------------------------ */
/* actions                                                             */
/* ------------------------------------------------------------------ */

function setBusy(busy, message) {
  el('estimate-btn').disabled = busy;
  el('plan-btn').disabled = busy;
  if (busy) {
    el('loading-text').textContent = message;
    show(el('placeholder'), false);
    show(el('estimate'), false);
    show(el('plan'), false);
  }
  show(el('loading'), busy);
}

async function submitEstimate() {
  showErrors([]);
  setBusy(true, 'Costing your trip…');
  try {
    renderEstimate(await postJson(`${API}/estimate`, readForm()));
    show(el('estimate'), true);
  } catch (error) {
    showErrors(String(error.message).split('\n'));
    show(el('placeholder'), true);
  } finally {
    setBusy(false);
  }
}

async function submitPlan(refresh = false) {
  showErrors([]);
  setBusy(true, refresh ? 'Rebuilding your itinerary…' : 'Planning your trip…');
  try {
    const url = `${API}/itineraries${refresh ? '?refresh=true' : ''}`;
    const { itinerary } = await postJson(url, readForm());
    renderPlan(itinerary);
    show(el('plan'), true);
    el('plan').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    showErrors(String(error.message).split('\n'));
    show(el('placeholder'), true);
  } finally {
    setBusy(false);
  }
}

form.addEventListener('submit', (event) => { event.preventDefault(); submitPlan(false); });
el('estimate-btn').addEventListener('click', () => submitEstimate());

/* Keep the return date from drifting behind the departure date. */
form.start_date.addEventListener('change', () => {
  form.end_date.min = form.start_date.value;
  if (form.end_date.value < form.start_date.value) form.end_date.value = form.start_date.value;
});

bootstrap();
