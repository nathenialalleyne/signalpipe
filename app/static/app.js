/* Signalpipe UI. Plain JS, no build step. Everything that came from the web is escaped before it is rendered. */
(() => {
'use strict';
const root = document.getElementById('root');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const href = (u) => (/^(https?:|mailto:|tel:)/i.test(u || '') ? esc(u) : '#');
const lines = (v) => (v || []).join('\n');
const unlines = (s) => String(s || '').split('\n').map((x) => x.trim()).filter(Boolean);
const ago = (ts) => { if (!ts) return 'never'; const d = (Date.now() / 1000 - ts) / 86400; return d < 1 / 24 ? 'just now' : d < 1 ? `${Math.round(d * 24)}h ago` : `${Math.round(d)}d ago`; };
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
const S = { status: null, view: 'today', leadTab: 'new', lead: null, leadId: null, outTab: 'email', outDone: false, playId: null, minScore: 0, q: '' };

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), 4200);
}
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.raw !== undefined) { init.body = opts.raw; init.headers['Content-Type'] = 'text/plain'; }
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const r = await fetch('/api' + path, init);
  if (r.status === 401) { renderLogin(); throw new Error('Sign in required'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { if (e.message !== 'Sign in required') toast(e.message); } };

/* ---------- small renderers ---------- */
const SIG = { agent_found: ['Found by agent', 'mint'], hiring: ['Hiring', 'clay'], funding: ['Funding', 'mint'], competitor_mention: ['Competitor mention', 'lilac'], competitor_engagement: ['Competitor engagement', 'lilac'], keyword_intent: ['Asked in public', 'sky'], keyword_post: ['Posted about it', 'sky'], repo_star: ['Starred a repo', 'peri'], launch: ['Launch', 'peri'], import: ['Imported', ''] };
const sigPill = (t) => { const [l, c] = SIG[t] || [t || 'Signal', '']; return `<span class="pill ${c}">${esc(l)}</span>`; };
const scoreBox = (raw) => { const n = Math.round(raw); const c = n >= 65 ? 'hi' : n >= 45 ? 'mid' : 'lo'; const w = n >= 65 ? 'Strong' : n >= 45 ? 'Fair' : 'Weak'; return `<div class="score ${c}"><b>${n}</b><span>${w}</span></div>`; };
const emailPill = (st, detail) => {
  if (!st) return '';
  const f = detail?.flags || [];
  const risky = f.includes('pattern_confirmed') ? 'Likely, pattern confirmed' : f.includes('pattern_guess') ? 'Risky, pattern guess' : f.includes('catch_all') ? 'Risky, accepts all mail' : 'Risky';
  const m = { valid: [f.includes('smtp_verified') ? 'Valid, mailbox confirmed' : 'Valid', 'mint'], risky: [risky, 'butter'], invalid: ['Invalid', 'blush'], unknown: ['Unchecked', ''] }[st] || [st, ''];
  return `<span class="pill ${m[1]}" title="${esc(detail?.reason || '')}">${esc(m[0])}</span>`;
};
const phonePill = (d) => {
  if (!d || !d.valid) return d && d.input ? '<span class="pill blush">Not a valid number</span>' : '';
  const type = { mobile: 'Mobile', landline: 'Landline', landline_or_mobile: 'Direct line', toll_free: 'Toll free', voip: 'VoIP', company_switchboard: 'Switchboard' }[d.type] || 'Phone';
  const w = d.call_window || {};
  const win = w.open_now === true ? `<span class="pill mint">In hours, ${esc(w.local_time)} local</span>` : w.open_now === false ? `<span class="pill butter">Closed, ${esc(w.local_time)} local</span>` : '';
  const flags = (d.flags || []).includes('tcpa_manual_dial_only') ? '<span class="pill sky">Dial by hand</span>' : '';
  return `<span class="pill sky">${esc(type)}${d.location ? ', ' + esc(d.location) : ''}</span>${win}${flags}`;
};
const empty = (title, body, action = '') => `<div class="empty col" style="align-items:center"><h3>${esc(title)}</h3><p>${esc(body)}</p>${action}</div>`;

/* ---------- frame ---------- */
const NAV = [['today', 'Today'], ['leads', 'Leads'], ['outreach', 'Outreach'], ['inbox', 'Inbox'], ['agents', 'Agents'], ['insights', 'Insights'], ['seo', 'SEO'], ['plays', 'Plays'], ['brain', 'Brain'], ['settings', 'Settings']];
function frame(inner) {
  const st = S.status || { leads: {}, config: {}, linkedin: {} };
  const counts = { leads: st.leads?.new || '', inbox: st.unhandled_replies || '' };
  const li = st.linkedin?.enabled;
  root.innerHTML = `<div class="app">
  <nav class="side" aria-label="Main">
    <div class="brand"><svg aria-hidden="true" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke-width="2.2" stroke-linecap="round"><path d="M4 18c3 0 3-12 6-12s3 12 6 12 3-6 4-6"/></svg>Signalpipe</div>
    <div class="nav">${NAV.map(([k, l]) => `<a href="#/${k}" ${S.view === k ? 'aria-current="page"' : ''}><span>${l}</span>${counts[k] ? `<span class="mono">${counts[k]}</span>` : ''}</a>`).join('')}</div>
    <div class="side-card">
      <div class="eyebrow">Today</div>
      <div class="row between"><span>Emails sent</span><span class="mono">${st.emails_today ?? 0} / ${st.config?.email_daily_cap ?? 0}</span></div>
      <div class="row between"><span>LinkedIn steps</span><span class="mono">${st.linkedin_today ?? 0} / ${st.config?.linkedin_daily_cap ?? 0}</span></div>
      <div class="row between"><span>Calls logged</span><span class="mono">${st.calls_today ?? 0}</span></div>
      <div class="row" style="gap:8px"><span class="dot ${li ? (st.linkedin.agent?.blocked ? 'bad' : '') : 'off'}" aria-hidden="true"></span>${li ? (st.linkedin.agent?.blocked ? 'LinkedIn needs you' : st.linkedin.via === 'agent' && !st.linkedin.agent.online ? 'LinkedIn agent offline' : 'LinkedIn connected') : 'LinkedIn by hand'}</div>
    </div>
  </nav>
  <main class="main" id="view">${inner}</main></div>`;
}
async function renderLogin() {
  const m = await fetch('/api/mode').then((r) => r.json()).catch(() => ({}));
  if (m.local) {
    root.innerHTML = `<div class="login"><div class="card lg col" style="width:min(420px,100%)"><div class="brand" style="padding:0">Signalpipe</div>
      <p>Signalpipe is running on this computer. To open it, start Signalpipe again the way you started it; it opens this page signed in.</p></div></div>`;
    return;
  }
  root.innerHTML = `<div class="login"><form class="card lg col" style="width:min(380px,100%)" data-form="login">
    <div class="brand" style="padding:0">Signalpipe</div>
    <label class="f">Password<input class="i" type="password" name="password" autocomplete="current-password" required></label>
    <button class="btn primary">Sign in</button></form></div>`;
}
const head = (eyebrow, title, right = '') => `<header class="row between" style="align-items:flex-end"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1></div><div class="row">${right}</div></header>`;

/* ---------- today ---------- */
async function viewToday() {
  const [st, runs, plays] = await Promise.all([api('/status'), api('/runs?limit=6'), api('/plays')]);
  S.status = st;
  const c = st.config, L = st.leads || {};
  const steps = [
    [st.brain_ready, 'Teach it your business', 'Enter your website so it learns what you sell and who buys.', '#/brain'],
    [st.plays > 0, 'Create a play', 'A play is a standing search for one kind of buyer.', '#/plays'],
    [c.llm, 'Connect Claude', 'An API key, or your Claude subscription through Claude Code. Without it there are no agents, scoring is keyword matching and messages are templates.', '#/settings'],
    [c.smtp, 'Connect your mailbox', 'Emails go out from your own address.', '#/settings'],
    [st.linkedin?.enabled, 'Connect LinkedIn', 'Optional. Without it you send LinkedIn steps by hand from a queue.', '#/settings'],
  ];
  const todo = steps.filter((s) => !s[0]);
  frame(`${head('Overview', 'Today', plays.length ? `<button class="btn primary" data-act="runAll">Run all plays now</button>` : '')}
  ${st.llm_error ? `<div class="card tight"><span class="pill blush">Claude</span> ${esc(st.llm_error)}</div>` : ''}
  ${st.linkedin?.error ? `<div class="card tight"><span class="pill blush">LinkedIn</span> ${esc(st.linkedin.error)}</div>` : ''}
  ${todo.length ? `<section class="card lg col"><h2>Finish setting up</h2><ul class="clean">${steps.map(([ok, t, d, h]) => `<li class="row"><span class="pill ${ok ? 'mint' : 'draft'}">${ok ? 'Done' : 'To do'}</span><a href="${h}" style="font-weight:600">${esc(t)}</a><span class="soft small">${esc(d)}</span></li>`).join('')}</ul></section>` : ''}
  <div class="stats">${[['New leads', L.new || 0, 'Waiting for review'], ['Approved', L.approved || 0, 'Sequence drafted'], ['Contacted', L.contacted || 0, 'In sequence'], ['Replied', L.replied || 0, `${st.unhandled_replies} need an answer`], ['Won', L.won || 0, 'Meetings booked'], ['Signals', st.signals_7d, 'Last 7 days']].map(([l, v, n]) => `<div class="card stat"><div class="small soft">${l}</div><div class="v">${v}</div><div class="small soft">${esc(n)}</div></div>`).join('')}</div>
  <section class="col"><h2>Recent runs</h2>${runs.length ? `<div class="card scroll"><table class="t"><thead><tr><th>Play</th><th>When</th><th>New signals</th><th>New leads</th><th>Enriched</th><th>Sources</th></tr></thead><tbody>${runs.map((r) => `<tr><td>${esc(r.play_name || '')}</td><td>${r.finished_at ? ago(r.finished_at) : '<span class="pill peri">Running</span>'}</td><td class="mono">${r.stats.new_signals ?? ''}</td><td class="mono">${r.stats.new_leads ?? ''}</td><td class="mono">${r.stats.enriched ?? ''}</td><td class="small soft">${r.error ? `<span class="pill blush">Failed</span> ${esc(r.error)}` : Object.entries(r.stats.sources || {}).map(([k, v]) => `${esc(k)} ${typeof v === 'string' ? 'failed' : v.new}`).join(' · ')}</td></tr>`).join('')}</tbody></table></div>` : empty('No runs yet', 'Create a play and run it to pull your first signals.')}</section>`);
}

/* ---------- leads ---------- */
async function viewLeads() {
  const [st, data] = await Promise.all([api('/status'), api(`/leads?status=${S.leadTab}&min_score=${S.minScore}&q=${encodeURIComponent(S.q)}&limit=150`)]);
  S.status = st;
  const L = st.leads || {};
  const tabs = [['new', 'New'], ['approved', 'Approved'], ['contacted', 'Contacted'], ['replied', 'Replied'], ['won', 'Won'], ['rejected', 'Rejected']];
  if (!S.leadId && data.leads[0]) S.leadId = data.leads[0].id;
  const list = data.leads.map((l) => {
    const c = l.contact || {};
    const ch = [l.has.email ? 'Email' : '', l.has.phone ? 'Phone' : '', l.has.linkedin ? 'LinkedIn' : ''].filter(Boolean).join(' · ') || 'No contact details yet';
    return `<button class="card lead ${l.id === S.leadId ? 'sel' : ''}" data-act="pick" data-id="${l.id}">${scoreBox(l.score)}
      <span class="col grow" style="gap:6px"><span class="row" style="gap:8px"><h3>${esc(l.company_name || 'Unknown')}</h3><span class="mono soft">${esc(l.company_domain || '')}</span>${sigPill(l.top_signal)}${l.signal_count > 1 ? `<span class="pill">${l.signal_count} signals</span>` : ''}</span>
      <span class="small soft">${esc((l.reasons?.items || [])[0] || l.top_title || '')}</span>
      <span class="small"><b style="font-weight:500">${esc([c.name, c.title].filter(Boolean).join(', ') || 'No named contact')}</b> <span class="soft">· ${esc(ch)} · ${ago(l.last_signal_at)}</span></span></span></button>`;
  }).join('');
  frame(`${head(`${data.total} in this view`, 'Leads', `<a class="btn" href="/api/export.csv?status=${S.leadTab}">Export CSV</a>`)}
  <div class="row between"><div class="tabs">${tabs.map(([k, l]) => `<button class="tab" aria-pressed="${S.leadTab === k}" data-act="leadTab" data-id="${k}">${l} ${L[k] || 0}</button>`).join('')}</div>
  <form class="row" data-form="leadFilter"><input class="i" style="width:180px" name="q" placeholder="Search company" value="${esc(S.q)}" aria-label="Search company"><select class="i" style="width:150px" name="min" aria-label="Minimum score">${[0, 45, 65].map((v) => `<option value="${v}" ${S.minScore == v ? 'selected' : ''}>${v ? `Score ${v} and up` : 'Any score'}</option>`).join('')}</select><button class="btn sm">Filter</button></form></div>
  <div class="split"><section class="a col" aria-label="Queue">${list || empty('Nothing here', S.leadTab === 'new' ? 'Run a play to find leads, or import a CSV on the Plays page.' : 'No leads with this status yet.')}</section>
  <aside class="b" id="detail" aria-label="Lead detail"></aside></div>`);
  if (S.leadId) loadLead(S.leadId);
}
const researchPanel = (l) => {
  const r = l.research;
  if (!r) return '';
  return `<div class="col" style="gap:8px"><div class="eyebrow">What the People finder learned</div>${r.summary ? `<p class="small">${esc(r.summary)}</p>` : ''}
    ${(r.hooks || []).length ? `<ul class="clean small" style="gap:4px">${r.hooks.map((h) => `<li><span class="pill sky">Hook</span> ${esc(h)}</li>`).join('')}</ul>` : ''}
    ${(r.people || []).map((p) => `<div class="small"><b style="font-weight:600">${esc(p.name)}</b> <span class="soft">${esc(p.title || '')}</span>${p.why ? `<div class="soft">${esc(p.why)}</div>` : ''}${(p.evidence || []).length ? `<div>${p.evidence.slice(0, 3).map((u, i) => `<a href="${href(u)}" target="_blank" rel="noopener noreferrer">source ${i + 1}</a>`).join(' · ')}</div>` : ''}</div>`).join('')}
    ${r.at ? `<span class="small soft">Researched ${ago(r.at)}</span>` : ''}</div>`;
};
async function loadLead(id) {
  const l = await api('/leads/' + id);
  S.lead = l;
  const el = document.getElementById('detail');
  if (!el) return;
  const r = l.reasons?.items || [];
  const isNew = l.status === 'new';
  el.innerHTML = `<div class="card lg col" style="gap:18px;box-shadow:var(--shadow-md)">
    <div><div class="row between"><div class="eyebrow">${esc(l.status)} · scored by ${esc(l.reasons?.method === 'claude' ? 'Claude' : 'keywords')}</div>${l.company_domain ? `<a class="small" href="${href('https://' + l.company_domain)}" target="_blank" rel="noopener noreferrer">${esc(l.company_domain)}</a>` : ''}</div>
    <h2>${esc(l.company_name || 'Unknown')}</h2><p class="small soft" style="margin-top:6px">${esc(l.description || 'No description found on their site.')}</p></div>
    <div class="col" style="gap:8px">${[['Fit', l.fit], ['Intent', l.intent], ['Recency', l.recency]].map(([k, v]) => `<div class="bar"><span class="soft">${k}</span><i><b style="width:${Math.round(v)}%"></b></i><span class="mono" style="text-align:right">${Math.round(v)}</span></div>`).join('')}</div>
    ${r.length ? `<div class="note"><ul class="clean" style="gap:4px">${r.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}
    ${(l.tech || []).length ? `<div class="row" style="gap:6px">${l.tech.slice(0, 10).map((t) => `<span class="pill">${esc(t)}</span>`).join('')}</div>` : ''}
    <div class="col" style="gap:8px"><div class="eyebrow">Signals</div>${l.signals.map((s) => `<div class="small">${sigPill(s.type)} <a href="${href(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a> <span class="soft">${ago(s.occurred_at)}</span>${s.text ? `<div class="soft" style="margin-top:2px">${esc(s.text.slice(0, 220))}</div>` : ''}</div>`).join('')}</div>
    ${researchPanel(l)}
    <div class="col" style="gap:8px"><div class="row between"><div class="eyebrow">Contacts</div><span class="row" style="gap:6px"><button class="btn sm" data-act="research" data-id="${l.id}">${l.research ? 'Find people again' : 'Find people'}</button><button class="btn sm" data-act="addContact">Add contact</button></span></div>
    ${l.contacts.map((c) => `<div class="contact"><div class="row between"><b style="font-weight:600">${esc(c.name || c.title || 'Contact')}${c.is_primary ? ' <span class="pill peri">Primary</span>' : ''}</b><span class="small soft">${esc(c.name ? c.title || '' : '')}</span></div>
      ${c.email ? `<div class="row" style="gap:8px"><span class="mono">${esc(c.email)}</span>${emailPill(c.email_status, c.email_detail)}</div>` : ''}
      ${c.phone ? `<div class="row" style="gap:8px"><a class="mono" href="${href('tel:' + c.phone)}">${esc(c.phone_detail?.display || c.phone)}</a>${phonePill(c.phone_detail)}</div>` : ''}
      <div class="row" style="gap:8px">${c.linkedin_url ? `<a class="small" href="${href(c.linkedin_url)}" target="_blank" rel="noopener noreferrer">${c.linkedin_url.includes('/search/') ? 'Find on LinkedIn' : 'LinkedIn profile'}</a>` : ''}${c.is_primary ? '' : `<button class="btn sm" data-act="primary" data-id="${c.id}">Make primary</button>`}</div></div>`).join('') || `<p class="small soft">No contacts yet. <a href="${href(l.linkedin_search_url)}" target="_blank" rel="noopener noreferrer">Search LinkedIn for the buyer</a>.</p>`}</div>
    ${l.messages.length ? `<div class="col" style="gap:6px"><div class="row between"><div class="eyebrow">Sequence</div>${l.variant ? `<span class="pill lilac" title="Opening angle being tested on this lead">Test: ${esc({ signal: 'signal first', pain: 'pain first', question: 'question first', proof: 'proof first' }[l.variant] || l.variant)}</span>` : ''}</div>${l.messages.map((m) => `<div class="row small" style="gap:8px"><span class="pill ${m.status === 'sent' || m.status === 'done' ? 'mint' : m.status === 'queued' ? 'peri' : m.status === 'failed' ? 'blush' : 'draft'}">${esc(m.channel)} ${m.step} · ${esc(m.status)}</span><span class="soft">${esc(m.subject || '')}</span></div>`).join('')}<a class="small" href="#/outreach">Open in Outreach</a></div>` : ''}
    <div class="row" style="gap:8px">${isNew ? `<button class="btn primary grow" data-act="approve" data-id="${l.id}">Approve and draft sequence</button><button class="btn" data-act="reject" data-id="${l.id}">Reject</button>` : `<select class="i" style="width:auto" data-change="leadStatus" aria-label="Lead status">${['new', 'approved', 'contacted', 'replied', 'won', 'lost', 'rejected'].map((s) => `<option ${s === l.status ? 'selected' : ''}>${s}</option>`).join('')}</select><button class="btn" data-act="redraft" data-id="${l.id}">Rewrite sequence</button>`}
    <button class="btn" data-act="enrich" data-id="${l.id}">Look up again</button></div></div>`;
}

/* ---------- outreach ---------- */
const OUTCOMES = { call: [['meeting_booked', 'Meeting booked'], ['connected_interested', 'Interested'], ['voicemail', 'Left voicemail'], ['no_answer', 'No answer'], ['not_interested', 'Not interested'], ['bad_number', 'Wrong number'], ['do_not_call', 'Asked not to call']], linkedin: [['sent', 'Sent'], ['accepted', 'Accepted'], ['replied', 'Replied']] };
async function viewOutreach() {
  const [st, data] = await Promise.all([api('/status'), api(`/outreach/${S.outTab}?status=${S.outDone ? 'done' : 'open'}`)]);
  S.status = st;
  const liOn = st.linkedin?.enabled;
  const cap = data.daily_cap ? `${data.done_today} of ${data.daily_cap} done today` : `${data.done_today} logged today`;
  const who = (m) => esc([m.contact_name || m.contact_title, m.company_name].filter(Boolean).join(', '));
  const stPill = (m) => `<span class="pill ${{ queued: 'peri', sending: 'peri', sent: 'mint', done: 'mint', failed: 'blush', skipped: '' }[m.status] || 'draft'}">${esc(m.status === 'draft' ? 'Needs review' : m.status)}${m.due || m.status !== 'draft' ? '' : ', ' + when(m.scheduled_at)}</span>`;
  const editor = (m, withSubject) => S.outDone ? `<div class="well">${esc(m.body)}</div>${m.error ? `<div class="small soft">${esc(m.error)}</div>` : ''}${m.outcome && m.channel !== 'email' ? `<div class="small soft">Outcome: ${esc(m.outcome)}</div>` : ''}` : `${withSubject ? `<label class="f">Subject<input class="i" data-field="subject" data-id="${m.id}" value="${esc(m.subject || '')}"></label>` : ''}<label class="f">${m.channel === 'call' ? 'Script' : 'Message'}<textarea class="i" rows="${m.channel === 'call' ? 9 : 6}" data-field="body" data-id="${m.id}">${esc(m.body || '')}</textarea></label>${m.error ? `<div class="small"><span class="pill blush">Note</span> ${esc(m.error)}</div>` : ''}`;
  const cards = data.items.map((m) => {
    let top = '', actions = '';
    if (m.channel === 'email') {
      top = `<span class="mono">${esc(m.email || '')}</span>${emailPill(m.email_status)}<span class="pill">Step ${m.step}</span>`;
      actions = m.status === 'queued' ? `<button class="btn sm" data-act="msg" data-do="unqueue" data-id="${m.id}">Hold</button>` : `<button class="btn softp sm" data-act="msg" data-do="queue" data-id="${m.id}">Approve to send</button>${m.step === 1 ? `<button class="btn sm" data-act="msg" data-do="queue_all" data-id="${m.id}">Approve all 3 steps</button>` : ''}`;
    } else if (m.channel === 'linkedin') {
      top = `<span class="pill">${m.step === 1 ? 'Connection request' : 'After they accept'}</span>${m.step === 1 ? `<span class="mono soft">${(m.body || '').length} of ${st.linkedin?.note_max || 200} characters</span>` : ''}`;
      actions = `<a class="btn sm" href="${href(m.open_url)}" target="_blank" rel="noopener noreferrer">${(m.open_url || '').includes('/search/') ? 'Find the person' : 'Open profile'}</a><button class="btn sm" data-act="copy" data-id="${m.id}">Copy</button>${liOn && m.status !== 'queued' ? `<button class="btn softp sm" data-act="msg" data-do="queue" data-id="${m.id}">Send for me</button>` : ''}${m.status === 'queued' ? `<button class="btn sm" data-act="msg" data-do="unqueue" data-id="${m.id}">Hold</button>` : ''}<select class="i" style="width:auto;min-height:36px;padding:4px 10px" data-change="outcome" data-id="${m.id}" aria-label="Mark as"><option value="">I did it by hand</option>${OUTCOMES.linkedin.map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>`;
    } else {
      top = `<a class="mono" style="font-size:15px" href="${href('tel:' + (m.phone || ''))}">${esc(m.phone_detail?.display || m.phone || '')}</a>${phonePill(m.phone_detail)}`;
      actions = OUTCOMES.call.map(([k, l], i) => `<button class="btn sm ${i === 0 ? 'softp' : ''}" data-act="outcome" data-do="${k}" data-id="${m.id}">${l}</button>`).join('');
    }
    return `<article class="card col"><div class="row between"><b style="font-weight:600">${who(m)}</b>${stPill(m)}</div><div class="row" style="gap:8px">${top}</div>${editor(m, m.channel === 'email')}
      ${S.outDone ? '' : `<div class="row" style="gap:8px">${actions}<button class="btn sm" data-act="msg" data-do="skip" data-id="${m.id}">Skip</button></div>`}</article>`;
  }).join('');
  const hint = { email: st.config.smtp ? '' : 'No mailbox is connected, so approved emails will wait. Add SMTP settings to send.', linkedin: liOn ? (st.linkedin.via === 'agent' ? (st.linkedin.agent.online ? 'Approved steps are sent by the agent on your computer, a few at a time during working hours.' : 'Approved steps will go out the next time the agent on your computer is running.') : 'Approved steps are sent for you during working hours, a few at a time.') : 'No LinkedIn account is connected. Open the profile, paste the note, then mark it done.', call: 'Sorted so numbers in business hours come first. US mobiles must be dialed by hand.' }[S.outTab];
  frame(`${head(cap, 'Outreach', `<div class="tabs" role="group" aria-label="Sending mode"><button class="tab" aria-pressed="${st.send_mode !== 'auto'}" data-act="mode" data-id="copilot">Review each step</button><button class="tab" aria-pressed="${st.send_mode === 'auto'}" data-act="mode" data-id="auto">Send automatically</button></div>`)}
  <div class="row between"><div class="tabs">${[['email', 'Email'], ['linkedin', 'LinkedIn'], ['call', 'Calls']].map(([k, l]) => `<button class="tab" aria-pressed="${S.outTab === k}" data-act="outTab" data-id="${k}">${l}</button>`).join('')}</div>
  <div class="tabs"><button class="tab" aria-pressed="${!S.outDone}" data-act="outDone" data-id="0">To do</button><button class="tab" aria-pressed="${S.outDone}" data-act="outDone" data-id="1">Done</button></div></div>
  ${hint ? `<p class="small soft">${esc(hint)}</p>` : ''}
  <div class="grid3">${cards || ''}</div>${cards ? '' : empty('Nothing queued', 'Approve a lead and its sequence appears here.')}`);
}

/* ---------- inbox ---------- */
async function viewInbox() {
  const [st, items] = await Promise.all([api('/status'), api('/replies?handled=0')]);
  S.status = st;
  const KIND = { bounce: ['Bounced', 'blush'], unsubscribe: ['Unsubscribed', 'butter'], auto_reply: ['Auto reply', ''], 'reply:interested': ['Interested', 'mint'], 'reply:question': ['Question', 'sky'], 'reply:not_now': ['Not now', 'butter'], 'reply:not_interested': ['Not interested', 'blush'], 'reply:referral': ['Referral', 'lilac'] };
  frame(`${head(st.config.imap ? 'Checked every 10 minutes' : 'Mailbox not connected', 'Inbox', `<button class="btn" data-act="poll">Check now</button>`)}
  <div class="col" style="gap:16px">${items.map((r) => { const [l, c] = KIND[r.kind] || ['Reply', 'peri']; const to = r.contact_email || ''; return `<article class="card col"><div class="row between"><b style="font-weight:600">${esc(r.contact_name || r.from_addr)} <span class="soft" style="font-weight:400">${esc(r.company_name || '')}</span></b><span class="row" style="gap:8px"><span class="pill ${c}">${l}</span><span class="small soft">${ago(r.received_at)}</span></span></div>
    <div class="small soft">${esc(r.subject || '')}</div><div class="well">${esc(r.body || '')}</div>
    ${r.kind.startsWith('reply') || r.kind === 'reply' ? `<label class="f">Your answer<textarea class="i" rows="5" data-reply="${r.id}">${esc(r.draft_reply || '')}</textarea></label>` : ''}
    <div class="row" style="gap:8px">${to && r.kind.startsWith('reply') ? `<button class="btn softp sm" data-act="mailto" data-id="${r.id}" data-to="${esc(to)}" data-subject="${esc(r.subject || '')}">Open in my mail app</button>` : ''}<button class="btn sm" data-act="copyReply" data-id="${r.id}">Copy</button><button class="btn sm" data-act="handled" data-id="${r.id}">Mark handled</button></div></article>`; }).join('') || empty('Inbox is clear', 'Replies, bounces and unsubscribe requests show up here, each with a drafted answer.')}</div>`);
}

/* ---------- insights ---------- */
async function viewInsights() {
  const [plays, r, rv] = await Promise.all([api('/plays'), api('/insights' + (S.insightPlay ? `?play_id=${S.insightPlay}` : '')), api('/review')]);
  const KIND = { add_keyword: 'Add keyword', remove_keyword: 'Remove keyword', add_competitor: 'Add competitor', remove_competitor: 'Remove competitor', add_hiring_title: 'Add hiring signal', remove_hiring_title: 'Remove hiring signal', add_buyer_title: 'Add buyer title', remove_buyer_title: 'Remove buyer title', add_disqualifier: 'Add disqualifier', messaging_note: 'Messaging' };
  const pname = (id) => (plays.find((p) => p.id === id) || {}).name || '';
  const strategist = `<section class="card lg col"><div class="row between"><div><h2>Strategist</h2><p class="small soft">${rv.at ? `Last review ${ago(rv.at)}. It reviews again each week.` : rv.enough_data ? 'Reviews your results each week and proposes changes.' : 'Starts once about a dozen leads have been reviewed or contacted.'}</p></div>${rv.llm ? `<button class="btn" data-act="reviewRun">Review now</button>` : ''}</div>
    ${rv.suggestions.length ? `<div class="col" style="gap:10px">${rv.suggestions.map((s) => `<div class="agent"><div class="row between"><span class="row" style="gap:8px"><span class="pill lilac">${esc(KIND[s.kind] || s.kind)}</span><b style="font-weight:600">${esc(s.value)}</b>${s.play_id && pname(s.play_id) ? `<span class="small soft">in ${esc(pname(s.play_id))}</span>` : ''}</span><span class="row" style="gap:6px"><button class="btn sm softp" data-act="reviewDo" data-do="apply" data-id="${s.id}">Apply</button><button class="btn sm" data-act="reviewDo" data-do="dismiss" data-id="${s.id}">Dismiss</button></span></div><span class="small soft">${esc(s.why)}</span></div>`).join('')}</div>` : `<p class="small soft">${rv.at ? 'No open suggestions.' : ''}</p>`}</section>`;
  const f = r.funnel;
  const pct = (v) => (v === null || v === undefined ? 'n/a' : v + '%');
  const table = (title, note, rows) => `<section class="card lg col"><div><h2>${title}</h2><p class="small soft">${note}</p></div>
    ${rows.length ? `<div class="scroll"><table class="t fixed"><thead><tr><th>${title.replace(/^By /, '')}</th><th>Contacted</th><th>Replied</th><th>Reply rate</th><th>Interested</th><th>Accepted on LinkedIn</th><th>Won</th></tr></thead><tbody>
    ${rows.map((g) => `<tr><td style="font-weight:500">${esc(g.name)}</td><td class="mono">${g.contacted}</td><td class="mono">${g.replied}</td><td>${g.enough ? `<span class="pill ${g.reply_rate >= 15 ? 'mint' : g.reply_rate >= 5 ? 'butter' : 'blush'}">${g.reply_rate}%</span>` : `<span class="pill draft" title="Fewer than ${r.min_judge} contacted">${g.reply_rate}%, too few to judge</span>`}</td><td class="mono">${g.positive}</td><td class="mono">${g.invited ? `${g.accepted} of ${g.invited}` : 'none sent'}</td><td class="mono">${g.won}</td></tr>`).join('')}</tbody></table></div>` : '<p class="small soft">Nothing contacted yet.</p>'}</section>`;
  frame(`${head('What is working', 'Insights', `<select class="i" style="width:auto" data-change="insightPlay" aria-label="Play"><option value="0">All plays</option>${plays.map((p) => `<option value="${p.id}" ${S.insightPlay == p.id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>`)}
  <div class="stats">${[['Found', f.found, 'All leads'], ['Approved', pct(f.approval_rate), `${f.approved} of ${f.reviewed} reviewed`], ['Contacted', f.contacted, 'At least one step sent'], ['Replied', pct(f.reply_rate), `${f.replied} replies`], ['Interested', f.positive, 'Positive replies'], ['Won', f.won, 'Meetings booked']].map(([l, v, n]) => `<div class="card stat"><div class="small soft">${l}</div><div class="v">${v}</div><div class="small soft">${esc(n)}</div></div>`).join('')}</div>
  ${r.findings.length ? `<section class="note col" style="gap:6px"><b style="font-weight:600">What the numbers say</b><ul class="clean" style="gap:4px">${r.findings.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></section>` : `<p class="small soft">${r.enough_data ? 'No clear differences between groups yet.' : `Conclusions appear once about ${r.min_judge * 2} leads have been contacted; until then treat the rates below as noise.`}</p>`}
  ${strategist}
  ${table('By opening', 'Each approved lead is given one of four opening angles. Angles that earn replies are used more often automatically; the others keep getting a small share so they can prove themselves.', r.tables.angle)}
  <div class="grid2" style="grid-template-columns:repeat(auto-fit,minmax(min(560px,100%),1fr))">${table('By signal', 'Which kind of trigger produces buyers.', r.tables.signal)}${table('By buyer level', 'Who actually answers.', r.tables.title)}
  ${table('By score', 'If high scores do not reply more, the scoring needs your reject notes.', r.tables.score)}${table('By channel', 'Which mix of channels each lead received.', r.tables.channel)}</div>`);
}

/* ---------- seo ---------- */
async function viewSeo() {
  const [d, drafts, st] = await Promise.all([api('/seo/latest'), api('/seo/drafts'), api('/status')]);
  S.status = st;
  const r = d.latest;
  S.seoPlan = r?.content_plan || [];
  S.seoDrafts = drafts;
  const sev = { high: ['High', 'blush'], medium: ['Medium', 'butter'], low: ['Low', 'sky'] };
  const cov = { covered: ['Covered', 'mint'], mentioned: ['Mentioned only', 'butter'], missing: ['No page', 'blush'] };
  const tone = !r ? '' : r.score >= 80 ? 'mint' : r.score >= 55 ? 'butter' : 'blush';
  frame(`${head(r ? `${r.url} · ${r.pages_crawled} pages · ${ago(r.created_at)}` : 'Your own site', 'Search health', `<form class="row" data-form="seo"><input class="i" style="width:240px" name="url" placeholder="yourdomain.com" value="${esc(r?.url || '')}" aria-label="Site to audit"><button class="btn primary">${r ? 'Run audit again' : 'Run audit'}</button></form>`)}
  ${!r ? empty('No audit yet', 'Enter your site to check crawlability, on-page basics and which buyer searches you have no page for.') : `<div class="split"><div class="a col" style="gap:24px;flex-basis:380px;flex-grow:2">
    <section class="card lg row" style="gap:24px"><div class="dial" style="background:var(--${tone}-bg);color:var(--${tone}-ink)"><b>${r.score}</b><span>${r.score >= 80 ? 'Good' : r.score >= 55 ? 'Fair' : 'Poor'}</span></div>
      <div class="col grow" style="gap:6px">${[['Sitemap', r.sitemap ? `Found, ${r.sitemap_urls} URLs` : 'Missing', !!r.sitemap], ['robots.txt', r.robots_txt ? 'Present' : 'Missing', r.robots_txt], ['llms.txt', r.llms_txt ? 'Present' : 'Missing', r.llms_txt]].map(([k, v, ok]) => `<div class="row between small"><span class="soft">${k}</span><span class="pill ${ok ? 'mint' : 'butter'}">${esc(v)}</span></div>`).join('')}</div></section>
    <section class="col"><h2>Fix these first</h2>${r.issues.map((i) => `<details class="card tight"><summary class="row" style="cursor:pointer;gap:12px"><span class="pill ${sev[i.severity][1]}">${sev[i.severity][0]}</span><span class="grow">${esc(i.message)}</span><span class="mono soft">${i.urls.length ? (i.count || i.urls.length) + ' pages' : 'Site'}</span></summary>${i.urls.length ? `<ul class="clean small" style="margin-top:10px">${i.urls.map((u) => `<li><a href="${href(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join('')}</ul>` : ''}</details>`).join('') || empty('No issues found', 'Nothing to fix on the pages crawled.')}</section></div>
  <div class="b col" style="gap:24px;position:static;flex-basis:480px;flex-grow:3">
    <section class="card lg col"><h2>What buyers search for</h2>${r.coverage.length ? `<div class="scroll"><table class="t"><thead><tr><th>Phrase</th><th>Coverage</th><th>Page</th></tr></thead><tbody>${r.coverage.map((k) => `<tr><td style="font-weight:500">${esc(k.keyword)}</td><td><span class="pill ${cov[k.status][1]}">${cov[k.status][0]}</span></td><td class="mono soft">${k.pages[0] ? esc(k.pages[0].replace(r.url, '') || '/') : 'Write one'}</td></tr>`).join('')}</tbody></table></div>` : '<p class="small soft">Build the Brain first so there are target phrases to check against.</p>'}</section>
    <section class="col"><div class="row between"><h2>Pages worth writing</h2><span class="small soft">${r.plan_source === 'claude' ? 'Planned by Claude from your gaps and prospect questions' : 'Template plan. Add a Claude key for briefs written from prospect questions.'}</span></div>
      <div class="grid2">${r.content_plan.map((b, i) => `<article class="card col" style="gap:8px"><span class="pill lilac" style="align-self:flex-start">${esc(b.intent.replace('_', ' '))}</span><h3>${esc(b.title)}</h3><div class="mono soft">${esc(b.target_keyword)}</div><p class="small soft">${esc(b.why)}</p><details class="small"><summary style="cursor:pointer">Outline</summary><ul style="margin:8px 0 0;padding-left:18px">${b.outline.map((o) => `<li>${esc(o)}</li>`).join('')}</ul></details>${draftBox(b, i, st)}</article>`).join('')}</div></section></div></div>`}`);
}

const draftBox = (b, i, st) => {
  const dr = (S.seoDrafts || {})[b.title];
  if (dr) return `<details class="small"><summary style="cursor:pointer"><span class="pill mint">Draft ready</span> ${esc(dr.title)}, ${dr.markdown.split(/\s+/).length} words</summary><p class="soft" style="margin:8px 0">${esc(dr.meta_description)}</p><div class="well" style="max-height:360px;overflow:auto">${esc(dr.markdown)}</div><div class="row" style="gap:8px;margin-top:8px"><button class="btn sm" data-act="copyDraft" data-id="${i}">Copy markdown</button><button class="btn sm" data-act="seoDraft" data-id="${i}">Write again</button></div></details>`;
  return st.config.llm ? `<button class="btn sm softp" style="align-self:flex-start" data-act="seoDraft" data-id="${i}">Write the draft</button>` : '';
};

/* ---------- plays ---------- */
const PLAY_FIELDS = [['keywords', 'Pain keywords', 'What prospects type or say when they have the problem'], ['competitors', 'Competitors', 'Names to watch for in public conversations'], ['hiring_titles', 'Hiring signals', 'Job titles whose ads mean a company needs you'], ['funding_keywords', 'Funding focus', 'Industries to watch for new rounds'], ['watchlist', 'Target accounts', 'Domains whose job boards to watch, one per line'], ['linkedin_watch', 'LinkedIn pages to watch', 'Competitor company pages or profiles. Needs LinkedIn connected.'], ['github_repos', 'GitHub repos', 'owner/name, to catch new stargazers']];
async function viewPlays() {
  const [st, plays] = await Promise.all([api('/status'), api('/plays')]);
  S.status = st;
  if (!S.playId && plays[0]) S.playId = plays[0].id;
  const p = plays.find((x) => x.id === S.playId);
  const src = Object.keys(st.sources);
  const form = p ? `<form class="card lg col" data-form="play" data-id="${p.id}" style="gap:16px">
    <div class="row between"><label class="f grow">Name<input class="i" name="name" value="${esc(p.name)}"></label><label class="f">Status<select class="i" name="status"><option ${p.status === 'active' ? 'selected' : ''}>active</option><option ${p.status === 'paused' ? 'selected' : ''}>paused</option></select></label></div>
    <div class="grid2">${PLAY_FIELDS.map(([k, l, d]) => `<label class="f">${l}<textarea class="i" name="${k}" rows="4" placeholder="${esc(d)}">${esc(lines(p.config[k]))}</textarea></label>`).join('')}</div>
    <fieldset style="border:0;padding:0;margin:0" class="col"><legend class="eyebrow" style="margin-bottom:8px">Sources</legend>${src.map((s) => `<label class="row small" style="gap:8px;flex-wrap:nowrap;align-items:flex-start"><input type="checkbox" name="src" value="${s}" ${(p.config.sources || []).includes(s) ? 'checked' : ''} style="margin-top:4px"><span><b style="font-weight:600">${esc(s)}</b> <span class="soft">${esc(st.sources[s])}</span></span></label>`).join('')}</fieldset>
    <div class="row"><label class="f">Look back (days)<input class="i" style="width:120px" type="number" min="1" max="90" name="lookback_days" value="${p.config.lookback_days || 14}"></label>
    <fieldset style="border:0;padding:0;margin:0"><legend class="eyebrow" style="margin-bottom:8px">Channels to draft</legend><div class="row">${['email', 'linkedin', 'phone'].map((c) => `<label class="row small" style="gap:6px"><input type="checkbox" name="ch" value="${c}" ${(p.config.channels || []).includes(c) ? 'checked' : ''}>${c}</label>`).join('')}</div></fieldset></div>
    <div class="row between"><div class="row"><button class="btn primary">Save</button><button type="button" class="btn softp" data-act="runPlay" data-id="${p.id}" ${p.running ? 'disabled' : ''}>${p.running ? 'Running' : 'Run now'}</button><label class="btn">Import CSV<input type="file" accept=".csv,text/csv" class="hidden" data-change="csv" data-id="${p.id}"></label></div><button type="button" class="btn danger" data-act="delPlay" data-id="${p.id}">Delete play</button></div>
    <p class="small soft">CSV import accepts LinkedIn's own connections export, a Sales Navigator export, or any sheet with name, company, email, phone and profile URL columns.</p></form>` : '';
  frame(`${head('Standing searches for one kind of buyer', 'Plays', `<button class="btn primary" data-act="newPlay">New play</button>`)}
  ${plays.length ? `<div class="tabs">${plays.map((x) => `<button class="tab" aria-pressed="${x.id === S.playId}" data-act="pickPlay" data-id="${x.id}">${esc(x.name)} ${x.running ? '· running' : ''}</button>`).join('')}</div>${form}${p?.last_run ? `<p class="small soft">Last run ${ago(p.last_run.finished_at || p.last_run.started_at)}: ${p.last_run.stats.new_signals ?? 0} new signals, ${p.last_run.stats.new_leads ?? 0} new leads${p.last_run.stats.enrich_backlog ? `, ${p.last_run.stats.enrich_backlog} waiting for lookup on the next run` : ''}.</p>` : ''}` : empty('No plays yet', st.brain_ready ? 'Create one and it starts from what the Brain knows.' : 'Build the Brain first so a new play starts with sensible keywords.', `<a class="btn" href="#/brain">Go to Brain</a>`)}`);
}

/* ---------- brain ---------- */
const BRAIN_LISTS = [['value_props', 'Value props'], ['proof_points', 'Proof points'], ['pains', 'Pains you solve'], ['competitors', 'Competitors'], ['keywords', 'Pain keywords'], ['hiring_signals', 'Hiring signals'], ['objections', 'Objections']];
const ICP_LISTS = [['titles', 'Buyer titles'], ['industries', 'Industries'], ['company_sizes', 'Company sizes'], ['geos', 'Regions'], ['disqualifiers', 'Disqualifiers']];
async function viewBrain() {
  const b = await api('/brain');
  const t = (name, label, v, rows = 2) => `<label class="f">${label}<textarea class="i" name="${name}" rows="${rows}">${esc(v || '')}</textarea></label>`;
  frame(`${head('What you sell and who buys it', 'Brain')}
  <form class="card lg row" data-form="brainBuild"><label class="f grow">Your website<input class="i" name="url" placeholder="yourdomain.com" value="${esc(b.website || '')}"></label><button class="btn primary" style="align-self:flex-end">${b.one_liner ? 'Rebuild from site' : 'Build from site'}</button></form>
  ${b._source === 'heuristic' ? '<p class="small"><span class="pill butter">Limited</span> Built without Claude, so only keywords were read off the page. Fill in the rest by hand, or add an API key and rebuild.</p>' : ''}
  <form class="card lg col" data-form="brain" style="gap:16px">
    <div class="grid2"><label class="f">Company<input class="i" name="company" value="${esc(b.company)}"></label><label class="f">Tone<input class="i" name="tone" value="${esc(b.tone)}"></label></div>
    ${t('one_liner', 'What you do, in one line', b.one_liner)}${t('offer', 'Offer', b.offer)}
    <div class="eyebrow">Ideal customer</div><div class="grid2">${ICP_LISTS.map(([k, l]) => t('icp.' + k, l + ', one per line', lines(b.icp[k]), 4)).join('')}</div>
    <div class="eyebrow">Messaging</div><div class="grid2">${BRAIN_LISTS.map(([k, l]) => t(k, l + ', one per line', lines(b[k]), 5)).join('')}</div>
    <div class="eyebrow">Sender</div><div class="grid2"><label class="f">Your name<input class="i" name="sender_name" value="${esc(b.sender_name)}"></label><label class="f">Your title<input class="i" name="sender_title" value="${esc(b.sender_title)}"></label><label class="f">Calendar link<input class="i" name="calendar_link" value="${esc(b.calendar_link)}"></label></div>
    <div class="row"><button class="btn primary">Save</button></div></form>
  <section class="card lg col"><div><h2>Questions from replies</h2><p class="small soft">When a prospect asks something your profile does not answer, the Inbox agent adds it here instead of guessing. Answer once and every later reply uses it.</p></div>
    ${(b.faq || []).length ? (b.faq || []).map((f) => `<form class="col" data-form="faq" style="gap:8px"><input type="hidden" name="q" value="${esc(f.q)}"><div class="row between"><b style="font-weight:600">${esc(f.q)}</b>${f.a ? '<span class="pill mint">Answered</span>' : '<span class="pill butter">Needs your answer</span>'}</div><textarea class="i" name="a" rows="2" placeholder="Your answer, in a sentence or two">${esc(f.a || '')}</textarea><div class="row" style="gap:8px"><button class="btn sm">Save answer</button><button type="button" class="btn sm" data-act="faqRemove" data-q="${esc(f.q)}">Remove</button></div></form>`).join('') : '<p class="small soft">None yet.</p>'}
    <form class="row" data-form="faq" style="align-items:flex-end"><label class="f grow">Add a question yourself<input class="i" name="q" placeholder="Do you integrate with Salesforce?"></label><label class="f grow">Answer<input class="i" name="a"></label><button class="btn">Add</button></form></section>`);
}

/* ---------- agents ---------- */
const COPILOT_IDEAS = ['Find 10 companies like my best leads', 'Research the top 5 new leads', 'Why are my replies low?', 'Create a play for fintech startups hiring SDRs'];
async function viewAgents() {
  const [st, ov, cp] = await Promise.all([api('/status'), api('/agents'), api('/copilot')]);
  S.status = st;
  const u = ov.usage || {}, sub = ov.engine === 'claude_code';
  const spend = sub ? `On your Claude subscription through Claude Code. ${u.subscription_calls || 0} run${u.subscription_calls === 1 ? '' : 's'} this month; they count toward your plan's usage limits.` : `About $${(u.usd || 0).toFixed(2)} of Claude spend this month${ov.budget ? ` of a $${ov.budget.toFixed(0)} cap` : ', no cap'}, ${u.searches || 0} web searches.`;
  const chat = cp.log.map((m) => m.role === 'you' ? `<div class="msg you">${esc(m.text)}</div>` : m.role === 'note' ? `<div class="msg sys">${esc(m.text)}</div>` : `<div class="msg bot">${m.error ? '<span class="pill blush">Stopped</span> ' : ''}${esc(m.text)}${(m.steps || []).length ? `<div class="steps">${m.steps.map((t) => `<span class="pill">${esc(t.replace(/_/g, ' '))}</span>`).join('')}</div>` : ''}</div>`).join('');
  const roster = ov.agents.map((a) => `<div class="agent ${a.on ? '' : 'off'}"><div class="row between"><b style="font-weight:600">${esc(a.label)}</b><label class="switch"><input type="checkbox" data-change="agentOn" data-id="${a.key}" ${a.on ? 'checked' : ''}>${a.on ? 'On' : 'Off'}</label></div>
    <span class="small soft">${esc(a.what)}</span><span class="small mono soft">${a.runs ? `${a.runs} run${a.runs === 1 ? '' : 's'} this month${a.runs - a.ok ? `, ${a.runs - a.ok} stopped` : ''}${sub ? '' : `, $${(+a.usd).toFixed(2)}`}, last ${ago(a.last)}` : 'No runs this month'}</span></div>`).join('');
  const LABEL = Object.fromEntries(ov.agents.map((a) => [a.key, a.label]));
  const runs = ov.runs.map((r) => `<details class="card tight"><summary class="row" style="cursor:pointer;gap:10px;flex-wrap:nowrap"><span class="pill ${r.status === 'ok' ? 'mint' : r.status === 'running' ? 'peri' : 'blush'}">${esc(LABEL[r.agent] || r.agent)}</span><span class="grow small" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.summary || r.subject || '')}</span><span class="small soft mono" style="white-space:nowrap">${ago(r.started_at)}</span></summary>
    <div class="small soft" style="margin-top:8px">${esc(r.subject || '')}${r.usd ? ` · $${(+r.usd).toFixed(3)}` : ''}${r.searches ? ` · ${r.searches} searches` : ''}${r.finished_at ? ` · ${Math.max(1, Math.round(r.finished_at - r.started_at))}s` : ''}</div>
    ${(r.steps || []).length ? `<div class="steps-list">${r.steps.map((s) => `<div><span class="pill ${s.error ? 'blush' : ''}">${esc(s.tool.replace(/_/g, ' '))}</span> <code>${esc(s.input || '')}</code>${s.result ? `<div class="soft" style="margin:2px 0 0 8px">${esc(s.result)}</div>` : ''}</div>`).join('')}</div>` : ''}</details>`).join('');
  frame(`${head(spend, 'Agents', `<button class="btn" data-act="agentRefresh">Refresh</button>`)}
  ${!ov.llm ? `<div class="card tight"><span class="pill butter">Off</span> Connect Claude in <a href="#/settings">Settings</a> to start the agents: an API key, or your Claude subscription through Claude Code.</div>` : ''}
  ${ov.faq_open ? `<div class="card tight"><span class="pill butter">${ov.faq_open}</span> The Inbox agent has questions only you can answer. <a href="#/brain">Answer them on the Brain page</a>.</div>` : ''}
  <div class="split"><section class="a col" aria-label="Copilot"><div class="card lg col"><div class="row between"><h2>Copilot</h2>${cp.log.length ? '<button class="btn sm" data-act="copilotReset">New conversation</button>' : ''}</div>
    <div class="chat" id="chat">${chat || `<p class="small soft">Ask it to do things in plain words. It uses the same actions as the dashboard and can call the other agents. It never sends messages; that stays in Outreach.</p><div class="row" style="gap:6px">${COPILOT_IDEAS.map((t) => `<button class="btn sm" data-act="copilotSuggest" data-text="${esc(t)}">${esc(t)}</button>`).join('')}</div>`}</div>
    <form class="col" data-form="copilot" style="gap:8px"><textarea class="i" name="text" rows="2" placeholder="Find 15 logistics companies that raised money this month" aria-label="Message the Copilot" ${ov.llm ? '' : 'disabled'}></textarea><div class="row between"><span class="small soft">Enter to send, Shift+Enter for a new line</span><button class="btn primary" ${ov.llm ? '' : 'disabled'}>Send</button></div></form></div>
    <section class="col"><h2>Activity</h2>${runs || '<p class="small soft">Every agent run shows up here with each step it took.</p>'}</section></section>
  <aside class="b col" aria-label="Agents"><section class="card lg col" style="gap:10px"><h2>Your agents</h2>${roster}</section></aside></div>`);
  const c = document.getElementById('chat'); if (c) c.scrollTop = c.scrollHeight;
}

const claudeSettings = (c, cn, field, pill, u) => {
  const sub = (cn.SIGNALPIPE_ENGINE || 'api') === 'claude_code';
  const ccState = c.claude_code ? '<span class="pill mint">Claude Code found on this computer</span>' : '<span class="pill butter">Claude Code is not installed yet</span>';
  return `<section class="card lg col"><div class="row between"><h2>Claude</h2>${pill(c.llm, sub ? 'On, subscription' : 'On, API key')}</div>
    <p class="small soft">Runs the agents, scores leads against your customer profile and writes your messages. Pick how Signalpipe reaches Claude.</p>
    <form class="col" data-form="conn" style="gap:14px">
      <div class="row" style="gap:12px;align-items:stretch">
        <label class="choice"><input type="radio" name="SIGNALPIPE_ENGINE" value="claude_code" ${sub ? 'checked' : ''}><span class="col" style="gap:4px"><b style="font-weight:600">My Claude subscription</b><span class="small soft">Through Claude Code on this computer, signed in to your Pro or Max plan. No extra bill; it uses your plan's usage limits, which you share with claude.ai.</span></span></label>
        <label class="choice"><input type="radio" name="SIGNALPIPE_ENGINE" value="api" ${sub ? '' : 'checked'}><span class="col" style="gap:4px"><b style="font-weight:600">An API key</b><span class="small soft">Pay per use from the Claude Console. Works on a server too. The monthly cap below keeps spend in check.</span></span></label></div>
      <div class="col" style="gap:8px"><div class="eyebrow">Subscription</div><div class="row" style="gap:8px">${ccState}</div>
        ${c.claude_code ? '<p class="small soft">If it is not signed in yet, open a terminal, run <span class="mono">claude</span>, and sign in with your Claude account. That is all.</p>' : '<p class="small">Install it once in a terminal on this computer (in WSL on Windows), then run <span class="mono">claude</span> and sign in with your Claude account:</p><div class="well mono">curl -fsSL https://claude.ai/install.sh | bash</div><p class="small soft">Restart Signalpipe afterwards so it finds Claude Code.</p>'}
        <details><summary class="small" style="cursor:pointer">Signing in on a server instead</summary><p class="small soft" style="margin:8px 0">Run <span class="mono">claude setup-token</span> on any computer where you are signed in, and paste the token here.</p>${field('CLAUDE_CODE_OAUTH_TOKEN', 'Claude Code token', { secret: true })}</details></div>
      <div class="col" style="gap:8px"><div class="eyebrow">API key</div>${field('ANTHROPIC_API_KEY', 'API key', { secret: true, ph: 'sk-ant-...' })}</div>
      <div class="grid2"><label class="f">Model<select class="i" name="SIGNALPIPE_MODEL">${[['claude-opus-5-5', 'Opus 5.5, best results'], ['claude-sonnet-5-5', 'Sonnet 5.5, about half the cost'], ['claude-haiku-5-5', 'Haiku 5.5, cheapest, weaker research']].map(([v, l]) => `<option value="${v}" ${(cn.SIGNALPIPE_MODEL || 'claude-opus-5-5') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        ${field('RESEARCH_PER_RUN', 'Leads the People finder researches per play run', { type: 'number', ph: '5' })}
        ${field('AGENT_BUDGET_USD', 'Monthly cap on API spend, in dollars (0 for none)', { type: 'number', ph: '20', attrs: 'step="1" min="0"' })}</div>
      ${!sub && u.usd ? `<p class="small soft">About $${u.usd.toFixed(2)} spent this month.</p>` : ''}
      <div class="row"><button class="btn primary">Save</button></div></form></section>`;
};

/* ---------- settings ---------- */
async function viewSettings() {
  const [st, sup, cn] = await Promise.all([api('/status'), api('/suppression'), api('/connections')]);
  S.status = st;
  const c = st.config, li = st.linkedin || {}, lo = li.local_agent || {};
  const isSet = (k) => (cn._secrets_set || []).includes(k);
  const field = (k, label, opts = {}) => {
    const secret = opts.secret;
    const ph = secret ? (isSet(k) ? 'Saved. Type to replace.' : (opts.ph || '')) : (opts.ph || '');
    return `<label class="f ${opts.cls || ''}">${label}<input class="i" name="${k}" type="${secret ? 'password' : opts.type || 'text'}" ${secret ? 'autocomplete="new-password"' : ''} value="${secret ? '' : esc(cn[k] || '')}" placeholder="${esc(ph)}" ${opts.attrs || ''}></label>`;
  };
  const pill = (ok, on = 'On', off = 'Not set up') => `<span class="pill ${ok ? 'mint' : 'draft'}">${ok ? on : off}</span>`;
  const ag = li.agent || {};
  const liState = lo.available
    ? (!lo.on ? `<p class="small soft">Not connected. LinkedIn steps wait in a queue for you to send by hand.</p>
        <p class="small">Press Connect and a Chrome window opens on LinkedIn's sign-in page. Sign in there once; Signalpipe keeps that window and does the LinkedIn steps in it while this app is running. It is a separate Chrome profile, so your everyday browsing is not affected.</p>`
      : `<p class="small"><span class="pill ${ag.blocked ? 'blush' : ag.online ? 'mint' : 'butter'}">${ag.blocked ? 'Needs you' : ag.online ? 'Connected' : 'Starting'}</span> ${ag.blocked ? esc(ag.blocked) + '. Finish it in the Signalpipe Chrome window; it carries on by itself.' : ag.online ? `Working in its own Chrome window on this computer. ${li.sent_this_week} of ${li.weekly_cap} invites used this week.` : 'Opening its Chrome window. If LinkedIn shows its sign-in page, sign in there.'}</p>`)
    : li.via === 'agent' ? `<p class="small"><span class="pill ${ag.blocked ? 'blush' : ag.online ? 'mint' : 'butter'}">${ag.blocked ? 'Needs you' : ag.online ? 'Agent running' : 'Agent offline'}</span> ${ag.blocked ? esc(ag.blocked) + (ag.on_server ? '. Open the agent\'s screen below and sign in there; it carries on by itself afterwards.' : '. Start the agent again and sign in in its window.') : `${ag.on_server ? 'The agent on this server is doing the LinkedIn steps.' : 'Your own Chrome is doing the LinkedIn steps.'} Last check-in ${ago(ag.last_seen)}. ${li.sent_this_week} of ${li.weekly_cap} invites used this week.`}</p>`
    : li.via === 'provider' ? `<p class="small"><span class="pill mint">Connected</span> ${li.account ? 'Signed in as ' + esc(li.account) + '. ' : ''}${li.sent_this_week} of ${li.weekly_cap} invites used this week.</p>`
    : `<p class="small soft">Not connected, so LinkedIn steps wait in a queue for you to send by hand. See the README for running the LinkedIn agent with a server deployment.</p>`;
  frame(`${head(c.local ? 'Saved on this computer only' : 'Saved here; these take precedence over environment variables', 'Settings')}
  ${claudeSettings(c, cn, field, pill, st.usage || {})}

  <section class="card lg col"><div class="row between"><h2>Email</h2><span class="row" style="gap:6px">${pill(c.smtp, 'Sending on', 'Sending off')}${pill(c.imap, 'Replies on', 'Replies off')}</span></div>
    <p class="small soft">Emails go out from your own mailbox, and replies are read from it so a sequence stops when someone answers.</p>
    <form class="col" data-form="conn" id="mailForm" style="gap:12px">
      <div class="grid2">${field('SMTP_USER', 'Your email address', { type: 'email', ph: 'you@yourcompany.com', attrs: 'data-change="mailGuess"' })}${field('SMTP_PASS', 'App password', { secret: true })}</div>
      <div id="mailHint" class="small soft"></div>
      <div class="grid2">${field('SMTP_FROM_NAME', 'Name people see', { ph: 'Sam Lee' })}${field('POSTAL_ADDRESS', 'Postal address for the footer', { ph: 'Required in commercial email in the US' })}</div>
      <details><summary class="small" style="cursor:pointer">Server settings (filled in for you when we recognise your provider)</summary>
        <div class="grid2" style="margin-top:12px">${field('SMTP_HOST', 'Outgoing server (SMTP)')}${field('SMTP_PORT', 'Port', { type: 'number' })}
        <label class="f">Security<select class="i" name="SMTP_SECURITY">${['starttls', 'ssl', 'none'].map((v) => `<option ${(cn.SMTP_SECURITY || 'starttls') === v ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        ${field('IMAP_HOST', 'Incoming server (IMAP)')}${field('IMAP_PORT', 'Port', { type: 'number' })}${field('EMAIL_DAILY_CAP', 'Most emails per day', { type: 'number', ph: '30' })}</div></details>
      <div class="row"><button class="btn primary">Save</button><button type="button" class="btn" data-act="testMail">Save and test</button>${c.smtp ? '<button type="button" class="btn" data-act="testEmail">Send myself a test email</button>' : ''}</div>
      <div id="mailTest"></div></form></section>

  <section class="card lg col"><div class="row between"><h2>LinkedIn</h2>${lo.available ? (lo.on ? '<button class="btn" data-act="localLiStop">Disconnect</button>' : '<button class="btn primary" data-act="localLiStart">Connect LinkedIn</button>') : ''}</div>
    ${liState}
    ${lo.available ? `<details><summary class="small" style="cursor:pointer">Optional: let it sign itself back in</summary>
      <p class="small soft" style="margin:8px 0">Only needed if LinkedIn signs it out. These are typed into LinkedIn's own sign-in page in that window and are stored only on this computer. LinkedIn may still ask you for a code, which you enter in the window.</p>
      <form class="row" data-form="conn" style="align-items:flex-end">${field('LINKEDIN_EMAIL', 'LinkedIn email', { cls: 'grow' })}${field('LINKEDIN_PASSWORD', 'LinkedIn password', { secret: true, cls: 'grow' })}<button class="btn">Save</button></form></details>` : ''}
    <p class="small soft">Automated LinkedIn activity is against LinkedIn's terms. The caps are low on purpose, and it stops whenever LinkedIn asks for a security check.</p>
    ${ag.screen_url ? `<div class="row"><a class="btn ${ag.blocked ? 'primary' : ''}" href="${esc(ag.screen_url)}" target="_blank" rel="noopener">Open the agent's screen</a><span class="small soft">Its browser window on the server. Use it to sign in once, or when LinkedIn asks for a security check.</span></div>` : ''}
    <div class="row">${li.via === 'provider' ? '<button class="btn" data-act="liSync">Check for accepts and replies</button><button class="btn danger" data-act="liDisconnect">Stop using this account</button>' : li.provider_ready && !li.enabled ? '<button class="btn primary" data-act="liConnect">Connect LinkedIn</button><button class="btn" data-act="liRefresh">Check connection</button>' : ''}</div></section>

  <details class="card lg"><summary style="cursor:pointer"><h2 style="display:inline">More options</h2></summary>
    <form class="col" data-form="conn" style="gap:12px;margin-top:16px">
      <div class="grid2">${field('LINKEDIN_DAILY_CAP', 'Most LinkedIn invites per day', { type: 'number', ph: '20' })}${field('LINKEDIN_TZ', 'Your time zone (LinkedIn works your hours)', { ph: 'America/New_York' })}
      ${field('HUNTER_API_KEY', 'Hunter API key (optional, paid)', { secret: true })}</div>
      <div class="row"><button class="btn">Save</button></div></form>
    <ul class="clean small" style="margin-top:16px">
      <li class="row">${pill(c.smtp_probe)}<span>Mailbox verification with each address's own mail server (needs outbound port 25)</span></li>
      <li class="row">${pill(c.twilio_lookup)}<span>Carrier phone lookup through Twilio</span></li>
      ${c.local ? '' : `<li class="row">${pill(c.auth)}<span>Password sign-in</span></li>`}</ul></details>
  <section class="card lg col"><h2>Check an address or number</h2><form class="row" data-form="check"><input class="i grow" name="value" placeholder="name@company.com or +1 415 555 0132" aria-label="Email or phone"><button class="btn">Check</button></form><div id="checkOut"></div></section>
  <section class="card lg col"><h2>Do not contact</h2><form class="row" data-form="suppress"><input class="i grow" name="value" placeholder="address or whole domain" aria-label="Address or domain"><button class="btn">Add</button></form>
    ${sup.length ? `<ul class="clean small">${sup.map((s) => `<li class="row between"><span><span class="mono">${esc(s.value)}</span> <span class="soft">${esc(s.reason || '')}</span></span><button class="btn sm" data-act="unsuppress" data-id="${esc(s.value)}">Remove</button></li>`).join('')}</ul>` : '<p class="small soft">Empty. Unsubscribe replies are added here automatically.</p>'}</section>
  ${c.auth && !c.local ? '<div class="row"><button class="btn" data-act="logout">Sign out</button></div>' : ''}`);
}

/* ---------- router ---------- */
const VIEWS = { today: viewToday, leads: viewLeads, outreach: viewOutreach, inbox: viewInbox, agents: viewAgents, insights: viewInsights, seo: viewSeo, plays: viewPlays, brain: viewBrain, settings: viewSettings };
const render = guard(async () => {
  const name = (location.hash.replace(/^#\//, '').split('?')[0]) || 'today';
  S.view = VIEWS[name] ? name : 'today';
  if (location.hash.includes('linkedin=connected')) { await api('/linkedin/refresh', { method: 'POST' }).catch(() => {}); history.replaceState(null, '', '#/settings'); }
  await VIEWS[S.view]();
});
window.addEventListener('hashchange', render);

/* ---------- actions ---------- */
const saveField = async (id) => {
  const body = {};
  root.querySelectorAll(`[data-field][data-id="${id}"]`).forEach((el) => { body[el.dataset.field] = el.value; });
  return body;
};
const A = {
  pick: async (id) => { S.leadId = +id; root.querySelectorAll('.lead').forEach((b) => b.classList.toggle('sel', +b.dataset.id === S.leadId)); await loadLead(S.leadId); },
  leadTab: (id) => { S.leadTab = id; S.leadId = null; return render(); },
  approve: async (id, btn) => { btn.disabled = true; btn.textContent = S.status?.config?.llm ? 'Finding people and drafting, about a minute' : 'Drafting'; const r = await api(`/leads/${id}/approve`, { method: 'POST', body: {} }); const ppl = r.research && !r.research.error ? `Found ${r.research.people} people. ` : ''; toast(`${ppl}Approved. Drafted ${(r.sequence.created || []).length} steps${r.sequence.source === 'template' ? ' from templates' : r.sequence.source === 'writer agent' ? ' with the Writer' : ''}.`); S.leadId = null; return render(); },
  research: async (id, btn) => { btn.disabled = true; btn.textContent = 'Searching, about a minute'; const r = await api(`/leads/${id}/research`, { method: 'POST' }); toast(`Found ${r.result.people} people, ${r.result.added} new, using ${r.result.searches} searches.`); return loadLead(id); },
  agentRefresh: () => render(),
  copilotReset: async () => { if (!confirm('Start a new conversation? The Copilot forgets this one.')) return; await api('/copilot/reset', { method: 'POST' }); return render(); },
  copilotSuggest: async (_, btn) => { const i = root.querySelector('[data-form="copilot"] textarea'); if (i) { i.value = btn.dataset.text; i.focus(); } },
  seoDraft: async (id, btn) => { const b = (S.seoPlan || [])[+id]; if (!b) return; btn.disabled = true; btn.textContent = 'Researching and writing, a few minutes'; await api('/seo/draft', { method: 'POST', body: { brief: b } }); toast('Draft written.'); return render(); },
  copyDraft: async (id) => { const d = (S.seoDrafts || {})[(S.seoPlan || [])[+id]?.title]; if (!d) return; await navigator.clipboard.writeText(`# ${d.title}\n\n${d.markdown}`); toast('Copied as markdown.'); },
  faqRemove: async (_, btn) => { await api('/brain/faq', { method: 'POST', body: { q: btn.dataset.q, remove: true } }); return render(); },
  reviewRun: async (_, btn) => { btn.disabled = true; btn.textContent = 'Reviewing'; const r = await api('/review/run', { method: 'POST' }); toast(r.suggestions.length ? `${r.suggestions.length} suggestions.` : 'No changes suggested this time.'); return render(); },
  reviewDo: async (id, btn) => { await api(`/review/${id}/${btn.dataset.do}`, { method: 'POST' }); toast(btn.dataset.do === 'apply' ? 'Applied.' : 'Dismissed.'); return render(); },
  reject: async (id) => { await api(`/leads/${id}/reject`, { method: 'POST', body: {} }); S.leadId = null; return render(); },
  enrich: async (id, btn) => { btn.disabled = true; btn.textContent = 'Looking up'; const r = await api(`/leads/${id}/enrich`, { method: 'POST' }); toast(r.result.contacts_added ? `Found ${r.result.contacts_added} more contacts.` : 'Nothing new found.'); return loadLead(id); },
  redraft: async (id, btn) => { btn.disabled = true; await api(`/leads/${id}/redraft`, { method: 'POST' }); toast('Sequence rewritten.'); return loadLead(id); },
  primary: async (id) => { await api('/contacts/' + id, { method: 'PUT', body: { primary: true } }); return loadLead(S.leadId); },
  addContact: async () => {
    const name = prompt('Contact name'); if (name === null) return;
    const email = prompt('Email (optional)') || ''; const phone = prompt('Phone (optional)') || ''; const linkedin_url = prompt('LinkedIn profile URL (optional)') || '';
    await api(`/leads/${S.leadId}/contacts`, { method: 'POST', body: { name, email, phone, linkedin_url } }); return loadLead(S.leadId);
  },
  outTab: (id) => { S.outTab = id; return render(); },
  outDone: (id) => { S.outDone = id === '1'; return render(); },
  mode: async (id) => { await api('/settings', { method: 'PUT', body: { send_mode: id } }); toast(id === 'auto' ? 'First emails to verified addresses and LinkedIn invites to known profiles now send without review.' : 'Every step waits for your review.'); return render(); },
  msg: async (id, btn) => { await api('/messages/' + id, { method: 'PUT', body: { ...(await saveField(id)), action: btn.dataset.do } }); return render(); },
  outcome: async (id, btn) => { await api('/messages/' + id, { method: 'PUT', body: { ...(await saveField(id)), action: 'done', outcome: btn.dataset.do } }); return render(); },
  copy: async (id) => { const el = root.querySelector(`[data-field="body"][data-id="${id}"]`); await navigator.clipboard.writeText(el ? el.value : ''); toast('Copied.'); },
  copyReply: async (id) => { await navigator.clipboard.writeText(root.querySelector(`[data-reply="${id}"]`)?.value || ''); toast('Copied.'); },
  mailto: async (id, btn) => { const body = root.querySelector(`[data-reply="${id}"]`)?.value || ''; await api('/replies/' + id, { method: 'PUT', body: { draft_reply: body } }); const s = btn.dataset.subject || ''; location.href = `mailto:${encodeURIComponent(btn.dataset.to)}?subject=${encodeURIComponent(/^re:/i.test(s) ? s : 'Re: ' + s)}&body=${encodeURIComponent(body)}`; },
  handled: async (id) => { await api('/replies/' + id, { method: 'PUT', body: { handled: true } }); return render(); },
  poll: async () => { const r = await api('/replies/poll', { method: 'POST' }); toast(r.reason || r.error || `Checked ${r.checked} messages, ${r.handled} new.`); return render(); },
  newPlay: async () => { const name = prompt('Name this play', 'Companies hiring sales roles'); if (!name) return; const p = await api('/plays', { method: 'POST', body: { name } }); S.playId = p.id; return render(); },
  pickPlay: (id) => { S.playId = +id; return render(); },
  runPlay: async (id) => { await api(`/plays/${id}/run`, { method: 'POST', body: {} }); toast('Running. New leads appear on the Leads page in about a minute.'); setTimeout(render, 1500); },
  runAll: async () => { const ps = await api('/plays'); for (const p of ps) await api(`/plays/${p.id}/run`, { method: 'POST', body: {} }); toast('Running all plays.'); setTimeout(render, 1500); },
  delPlay: async (id) => { if (!confirm('Delete this play and all of its leads, contacts and messages? This cannot be undone.')) return; await api('/plays/' + id, { method: 'DELETE' }); S.playId = null; return render(); },
  testEmail: async () => { await api('/outreach/test-email', { method: 'POST', body: {} }); toast('Test email sent to your own address.'); },
  liConnect: async () => { const r = await api('/linkedin/connect', { method: 'POST' }); window.open(r.url, '_blank', 'noopener'); toast('Sign in on the page that opened, then press Check connection.'); },
  liRefresh: async () => { const r = await api('/linkedin/refresh', { method: 'POST' }); toast(r.connected ? 'LinkedIn is connected.' : r.reason); return render(); },
  liDisconnect: async () => { if (!confirm('Stop using this LinkedIn account here?')) return; await api('/linkedin/disconnect', { method: 'POST' }); return render(); },
  liSync: async () => { const r = await api('/linkedin/sync', { method: 'POST' }); toast(r.error || `${r.accepted || 0} accepted, ${r.replies || 0} replies.`); return render(); },
  unsuppress: async (id) => { await api('/suppression/' + encodeURIComponent(id), { method: 'DELETE' }); return render(); },
  localLiStart: async (_, btn) => { btn.disabled = true; await api('/local/linkedin/start', { method: 'POST' }); toast('Opening a Chrome window on LinkedIn. Sign in there once.'); setTimeout(render, 4000); return render(); },
  localLiStop: async () => { if (!confirm('Disconnect LinkedIn? Its Chrome window closes and LinkedIn steps wait in the queue for you.')) return; await api('/local/linkedin/stop', { method: 'POST' }); return render(); },
  testMail: async (_, btn) => {
    const f = document.getElementById('mailForm');
    btn.disabled = true; btn.textContent = 'Testing';
    await api('/connections', { method: 'PUT', body: Object.fromEntries(new FormData(f).entries()) });
    const r = await api('/connections/test-mail', { method: 'POST' });
    await render();
    const out = document.getElementById('mailTest');
    if (out) out.innerHTML = Object.entries(r).map(([k, v]) => `<div class="row small" style="gap:8px"><span class="pill ${v.ok ? 'mint' : 'blush'}">${k === 'sending' ? 'Sending' : 'Replies'}</span>${esc(v.message)}</div>`).join('');
  },
  logout: async () => { await api('/logout', { method: 'POST' }); renderLogin(); },
};
root.addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled) return;
  e.preventDefault();
  guard(A[b.dataset.act])(b.dataset.id, b);
});
root.addEventListener('change', guard(async (e) => {
  const el = e.target.closest('[data-change]');
  if (!el) return;
  if (el.dataset.change === 'mailGuess' && el.value.includes('@')) {
    const g = await api('/connections/mail-guess', { method: 'POST', body: { email: el.value } });
    const f = el.form, hint = document.getElementById('mailHint');
    if (g.settings) {
      for (const [k, v] of Object.entries(g.settings)) { const i = f.elements[k]; if (i && (!i.value || i.dataset.auto)) { i.value = v; i.dataset.auto = '1'; } }
    }
    if (hint) hint.innerHTML = g.provider ? `<span class="pill sky">${esc(g.label)}</span> ${esc(g.help)}` : esc(g.help);
    return;
  }
  if (el.dataset.change === 'insightPlay') { S.insightPlay = +el.value; return render(); }
  if (el.dataset.change === 'agentOn') { await api('/agents/' + el.dataset.id, { method: 'POST', body: { on: el.checked } }); return render(); }
  if (el.dataset.change === 'leadStatus') { await api(`/leads/${S.leadId}/status`, { method: 'POST', body: { status: el.value } }); return render(); }
  if (el.dataset.change === 'outcome' && el.value) { await api('/messages/' + el.dataset.id, { method: 'PUT', body: { ...(await saveField(el.dataset.id)), action: 'done', outcome: el.value } }); return render(); }
  if (el.dataset.change === 'csv' && el.files[0]) { const r = await api(`/plays/${el.dataset.id}/import`, { method: 'POST', raw: await el.files[0].text() }); toast(`Imported ${r.rows} rows, ${r.new_leads} new leads.`); return render(); }
}));
root.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && e.target.closest('[data-form="copilot"] textarea')) { e.preventDefault(); e.target.form.requestSubmit(); }
});
root.addEventListener('submit', guard(async (e) => {
  const f = e.target.closest('[data-form]');
  if (!f) return;
  e.preventDefault();
  const fd = new FormData(f), kind = f.dataset.form, btn = f.querySelector('button:not([type="button"])');
  if (kind === 'login') { await api('/login', { method: 'POST', body: { password: fd.get('password') } }); return render(); }
  if (kind === 'leadFilter') { S.q = fd.get('q'); S.minScore = +fd.get('min'); S.leadId = null; return render(); }
  if (kind === 'brainBuild') { btn.disabled = true; btn.textContent = 'Reading your site'; const r = await api('/brain/build', { method: 'POST', body: { url: fd.get('url') } }); toast(r.llm_error || 'Brain built. Review it, then create a play.'); return render(); }
  if (kind === 'brain') {
    const body = { icp: {} };
    for (const [k, v] of fd.entries()) { if (k.startsWith('icp.')) body.icp[k.slice(4)] = unlines(v); else if (BRAIN_LISTS.some(([n]) => n === k)) body[k] = unlines(v); else body[k] = v; }
    await api('/brain', { method: 'PUT', body }); return toast('Saved.');
  }
  if (kind === 'play') {
    const config = { sources: fd.getAll('src'), channels: fd.getAll('ch'), lookback_days: +fd.get('lookback_days') || 14 };
    PLAY_FIELDS.forEach(([k]) => { config[k] = unlines(fd.get(k)); });
    await api('/plays/' + f.dataset.id, { method: 'PUT', body: { name: fd.get('name'), status: fd.get('status'), config } }); toast('Saved.'); return render();
  }
  if (kind === 'seo') { btn.disabled = true; btn.textContent = 'Crawling, about a minute'; await api('/seo/audit', { method: 'POST', body: { url: fd.get('url') } }); return render(); }
  if (kind === 'conn') {
    await api('/connections', { method: 'PUT', body: Object.fromEntries(fd.entries()) });
    toast('Saved.');
    return render();
  }
  if (kind === 'copilot') {
    const text = String(fd.get('text') || '').trim(); if (!text) return;
    const chat = document.getElementById('chat'), ta = f.querySelector('textarea');
    if (chat) { if (!chat.querySelector('.msg')) chat.innerHTML = ''; chat.insertAdjacentHTML('beforeend', `<div class="msg you">${esc(text)}</div><div class="msg sys" id="thinking">Working on it. This can take a minute when it searches the web.</div>`); chat.scrollTop = chat.scrollHeight; }
    ta.value = ''; ta.disabled = true; btn.disabled = true;
    try { await api('/copilot', { method: 'POST', body: { text } }); } finally { await render(); }
    return root.querySelector('[data-form="copilot"] textarea')?.focus();
  }
  if (kind === 'faq') { if (!String(fd.get('q') || '').trim()) return; await api('/brain/faq', { method: 'POST', body: { q: fd.get('q'), a: fd.get('a') } }); toast('Saved. Replies will use this answer.'); return render(); }
  if (kind === 'suppress') { await api('/suppression', { method: 'POST', body: { value: fd.get('value') } }); return render(); }
  if (kind === 'check') {
    const v = String(fd.get('value')).trim(), out = document.getElementById('checkOut');
    if (v.includes('@')) { const [r] = await api('/validate/email', { method: 'POST', body: { email: v } }); out.innerHTML = `<div class="row" style="gap:8px"><span class="mono">${esc(r.email)}</span>${emailPill(r.status, r)}<span class="small soft">${esc(r.reason)} · confidence ${r.score}</span></div>`; }
    else { const [r] = await api('/validate/phone', { method: 'POST', body: { phone: v } }); out.innerHTML = r.valid ? `<div class="row" style="gap:8px"><span class="mono">${esc(r.display)}</span>${phonePill(r)}<span class="small soft">${esc(r.carrier || '')}</span></div>` : `<span class="pill blush">Not a valid number</span> <span class="small soft">${esc(r.reason || '')}</span>`; }
  }
}));
render();
})();
