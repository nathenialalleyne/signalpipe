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
const SIG = { hiring: ['Hiring', 'clay'], funding: ['Funding', 'mint'], competitor_mention: ['Competitor mention', 'lilac'], competitor_engagement: ['Competitor engagement', 'lilac'], keyword_intent: ['Asked in public', 'sky'], keyword_post: ['Posted about it', 'sky'], repo_star: ['Starred a repo', 'peri'], launch: ['Launch', 'peri'], import: ['Imported', ''] };
const sigPill = (t) => { const [l, c] = SIG[t] || [t || 'Signal', '']; return `<span class="pill ${c}">${esc(l)}</span>`; };
const scoreBox = (raw) => { const n = Math.round(raw); const c = n >= 65 ? 'hi' : n >= 45 ? 'mid' : 'lo'; const w = n >= 65 ? 'Strong' : n >= 45 ? 'Fair' : 'Weak'; return `<div class="score ${c}"><b>${n}</b><span>${w}</span></div>`; };
const emailPill = (st, detail) => {
  if (!st) return '';
  const guess = (detail?.flags || []).includes('pattern_guess');
  const m = { valid: ['Valid', 'mint'], risky: [guess ? 'Risky, pattern guess' : 'Risky', 'butter'], invalid: ['Invalid', 'blush'], unknown: ['Unchecked', ''] }[st] || [st, ''];
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
const NAV = [['today', 'Today'], ['leads', 'Leads'], ['outreach', 'Outreach'], ['inbox', 'Inbox'], ['seo', 'SEO'], ['plays', 'Plays'], ['brain', 'Brain'], ['settings', 'Settings']];
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
      <div class="row" style="gap:8px"><span class="dot ${li ? '' : 'off'}" aria-hidden="true"></span>${li ? 'LinkedIn connected' : 'LinkedIn by hand'}</div>
    </div>
  </nav>
  <main class="main" id="view">${inner}</main></div>`;
}
function renderLogin() {
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
    [c.llm, 'Add a Claude API key', 'Without it, scoring is keyword matching and messages are plain templates.', '#/settings'],
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
    <div class="col" style="gap:8px"><div class="row between"><div class="eyebrow">Contacts</div><button class="btn sm" data-act="addContact">Add contact</button></div>
    ${l.contacts.map((c) => `<div class="contact"><div class="row between"><b style="font-weight:600">${esc(c.name || c.title || 'Contact')}${c.is_primary ? ' <span class="pill peri">Primary</span>' : ''}</b><span class="small soft">${esc(c.name ? c.title || '' : '')}</span></div>
      ${c.email ? `<div class="row" style="gap:8px"><span class="mono">${esc(c.email)}</span>${emailPill(c.email_status, c.email_detail)}</div>` : ''}
      ${c.phone ? `<div class="row" style="gap:8px"><a class="mono" href="${href('tel:' + c.phone)}">${esc(c.phone_detail?.display || c.phone)}</a>${phonePill(c.phone_detail)}</div>` : ''}
      <div class="row" style="gap:8px">${c.linkedin_url ? `<a class="small" href="${href(c.linkedin_url)}" target="_blank" rel="noopener noreferrer">${c.linkedin_url.includes('/search/') ? 'Find on LinkedIn' : 'LinkedIn profile'}</a>` : ''}${c.is_primary ? '' : `<button class="btn sm" data-act="primary" data-id="${c.id}">Make primary</button>`}</div></div>`).join('') || `<p class="small soft">No contacts yet. <a href="${href(l.linkedin_search_url)}" target="_blank" rel="noopener noreferrer">Search LinkedIn for the buyer</a>.</p>`}</div>
    ${l.messages.length ? `<div class="col" style="gap:6px"><div class="eyebrow">Sequence</div>${l.messages.map((m) => `<div class="row small" style="gap:8px"><span class="pill ${m.status === 'sent' || m.status === 'done' ? 'mint' : m.status === 'queued' ? 'peri' : m.status === 'failed' ? 'blush' : 'draft'}">${esc(m.channel)} ${m.step} · ${esc(m.status)}</span><span class="soft">${esc(m.subject || '')}</span></div>`).join('')}<a class="small" href="#/outreach">Open in Outreach</a></div>` : ''}
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
  const stPill = (m) => `<span class="pill ${{ queued: 'peri', sent: 'mint', done: 'mint', failed: 'blush', skipped: '' }[m.status] || 'draft'}">${esc(m.status === 'draft' ? 'Needs review' : m.status)}${m.due || m.status !== 'draft' ? '' : ', ' + when(m.scheduled_at)}</span>`;
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
  const hint = { email: st.config.smtp ? '' : 'No mailbox is connected, so approved emails will wait. Add SMTP settings to send.', linkedin: liOn ? 'Approved steps are sent for you during working hours, a few at a time.' : 'No LinkedIn account is connected. Open the profile, paste the note, then mark it done.', call: 'Sorted so numbers in business hours come first. US mobiles must be dialed by hand.' }[S.outTab];
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

/* ---------- seo ---------- */
async function viewSeo() {
  const d = await api('/seo/latest');
  const r = d.latest;
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
      <div class="grid2">${r.content_plan.map((b) => `<article class="card col" style="gap:8px"><span class="pill lilac" style="align-self:flex-start">${esc(b.intent.replace('_', ' '))}</span><h3>${esc(b.title)}</h3><div class="mono soft">${esc(b.target_keyword)}</div><p class="small soft">${esc(b.why)}</p><details class="small"><summary style="cursor:pointer">Outline</summary><ul style="margin:8px 0 0;padding-left:18px">${b.outline.map((o) => `<li>${esc(o)}</li>`).join('')}</ul></details></article>`).join('')}</div></section></div></div>`}`);
}

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
    <div class="row"><button class="btn primary">Save</button></div></form>`);
}

/* ---------- settings ---------- */
async function viewSettings() {
  const [st, sup] = await Promise.all([api('/status'), api('/suppression')]);
  S.status = st;
  const c = st.config, li = st.linkedin || {};
  const rowS = (ok, name, on, off, env) => `<li class="row"><span class="pill ${ok ? 'mint' : 'draft'}">${ok ? 'On' : 'Off'}</span><b style="font-weight:600">${name}</b><span class="small soft grow">${esc(ok ? on : off)}</span><span class="mono soft">${esc(env)}</span></li>`;
  frame(`${head('Keys live in environment variables, never in the database', 'Settings')}
  <section class="card lg col"><h2>Connections</h2><ul class="clean">
    ${rowS(c.llm, 'Claude', `Scoring and writing with ${c.llm_model}`, 'Keyword scoring and template messages only', 'ANTHROPIC_API_KEY')}
    ${rowS(c.smtp, 'Outgoing mail', `Up to ${c.email_daily_cap} a day from your mailbox`, 'Approved emails will wait', 'SMTP_HOST SMTP_USER SMTP_PASS SMTP_FROM')}
    ${rowS(c.imap, 'Reply detection', 'Replies stop sequences automatically', 'Replies will not be detected', 'IMAP_HOST IMAP_USER IMAP_PASS')}
    ${rowS(c.hunter, 'Email finder and verifier', 'Hunter finds named contacts and verifies mailboxes', 'Addresses are pattern guesses checked against DNS only', 'HUNTER_API_KEY')}
    ${rowS(c.twilio_lookup, 'Carrier phone lookup', 'Twilio confirms line type per number', 'Line type comes from the number plan only', 'TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN')}
    ${rowS(c.postal_address, 'Postal address in footer', 'Included in every email', 'Required by US law for commercial email', 'POSTAL_ADDRESS')}
    ${rowS(c.auth, 'Password', 'Sign-in required', 'Anyone who can reach this address can use it', 'APP_PASSWORD')}</ul>
    ${c.smtp ? '<div class="row"><button class="btn sm" data-act="testEmail">Send myself a test email</button></div>' : ''}</section>
  <section class="card lg col"><h2>LinkedIn</h2>
    <p class="small soft">${li.enabled ? `Connected${li.account ? ' as ' + esc(li.account) : ''}. ${li.sent_this_week} of ${li.weekly_cap} invites used this week.` : li.provider_ready ? 'Provider keys are set. Sign in to LinkedIn on the provider page, then come back and press Check connection.' : 'To send and read LinkedIn automatically, add a Unipile account (UNIPILE_DSN and UNIPILE_API_KEY), then connect here. Without it, LinkedIn steps wait in a queue for you to send by hand.'}</p>
    <p class="small soft">Automated LinkedIn activity is against LinkedIn's terms for third-party tools. The caps here are set low on purpose.</p>
    <div class="row">${li.enabled ? '<button class="btn" data-act="liSync">Check for accepts and replies</button><button class="btn danger" data-act="liDisconnect">Stop using this account</button>' : li.provider_ready ? '<button class="btn primary" data-act="liConnect">Connect LinkedIn</button><button class="btn" data-act="liRefresh">Check connection</button>' : ''}</div></section>
  <section class="card lg col"><h2>Check an address or number</h2><form class="row" data-form="check"><input class="i grow" name="value" placeholder="name@company.com or +1 415 555 0132" aria-label="Email or phone"><button class="btn">Check</button></form><div id="checkOut"></div></section>
  <section class="card lg col"><h2>Do not contact</h2><form class="row" data-form="suppress"><input class="i grow" name="value" placeholder="address or whole domain" aria-label="Address or domain"><button class="btn">Add</button></form>
    ${sup.length ? `<ul class="clean small">${sup.map((s) => `<li class="row between"><span><span class="mono">${esc(s.value)}</span> <span class="soft">${esc(s.reason || '')}</span></span><button class="btn sm" data-act="unsuppress" data-id="${esc(s.value)}">Remove</button></li>`).join('')}</ul>` : '<p class="small soft">Empty. Unsubscribe replies are added here automatically.</p>'}</section>
  ${c.auth ? '<div class="row"><button class="btn" data-act="logout">Sign out</button></div>' : ''}`);
}

/* ---------- router ---------- */
const VIEWS = { today: viewToday, leads: viewLeads, outreach: viewOutreach, inbox: viewInbox, seo: viewSeo, plays: viewPlays, brain: viewBrain, settings: viewSettings };
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
  approve: async (id, btn) => { btn.disabled = true; btn.textContent = 'Drafting'; const r = await api(`/leads/${id}/approve`, { method: 'POST', body: {} }); toast(`Approved. Drafted ${(r.sequence.created || []).length} steps${r.sequence.source === 'template' ? ' from templates' : ''}.`); S.leadId = null; return render(); },
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
  mode: async (id) => { await api('/settings', { method: 'PUT', body: { send_mode: id } }); toast(id === 'auto' ? 'First emails to verified addresses now send without review.' : 'Every step waits for your review.'); return render(); },
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
  if (el.dataset.change === 'leadStatus') { await api(`/leads/${S.leadId}/status`, { method: 'POST', body: { status: el.value } }); return render(); }
  if (el.dataset.change === 'outcome' && el.value) { await api('/messages/' + el.dataset.id, { method: 'PUT', body: { ...(await saveField(el.dataset.id)), action: 'done', outcome: el.value } }); return render(); }
  if (el.dataset.change === 'csv' && el.files[0]) { const r = await api(`/plays/${el.dataset.id}/import`, { method: 'POST', raw: await el.files[0].text() }); toast(`Imported ${r.rows} rows, ${r.new_leads} new leads.`); return render(); }
}));
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
  if (kind === 'suppress') { await api('/suppression', { method: 'POST', body: { value: fd.get('value') } }); return render(); }
  if (kind === 'check') {
    const v = String(fd.get('value')).trim(), out = document.getElementById('checkOut');
    if (v.includes('@')) { const [r] = await api('/validate/email', { method: 'POST', body: { email: v } }); out.innerHTML = `<div class="row" style="gap:8px"><span class="mono">${esc(r.email)}</span>${emailPill(r.status, r)}<span class="small soft">${esc(r.reason)} · confidence ${r.score}</span></div>`; }
    else { const [r] = await api('/validate/phone', { method: 'POST', body: { phone: v } }); out.innerHTML = r.valid ? `<div class="row" style="gap:8px"><span class="mono">${esc(r.display)}</span>${phonePill(r)}<span class="small soft">${esc(r.carrier || '')}</span></div>` : `<span class="pill blush">Not a valid number</span> <span class="small soft">${esc(r.reason || '')}</span>`; }
  }
}));
render();
})();
