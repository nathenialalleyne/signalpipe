// A stand-in for LinkedIn used by the agent's end-to-end test. It reproduces the page structure the agent
// relies on (button names, dialogs, lists) and records what the agent did. It is NOT LinkedIn: a pass here
// proves the agent's logic and its protocol with Signalpipe, not that LinkedIn's real pages match.
import http from 'node:http';

const port = Number(process.argv[2] || 8931);
const PEOPLE = {
  'dana-x': { name: 'Dana Reyes', headline: 'Head of Sales at Acme', degree: '2nd' },
  'more-menu': { name: 'Lee Park', headline: 'CEO at Beta Corp', degree: '3rd', connectInMore: true },
  'ben-first': { name: 'Ben Ode', headline: 'Founder at Initech', degree: '1st' },
  'limit-user': { name: 'Lim Itt', headline: 'COO at Capped', degree: '2nd', limit: true },
};
const S = { invites: [], messages: [], pending: [], connected: ['ben-first'], threads: { 'ben-first': ['Hi Ben, good to meet you at the conference.'] }, logins: 0 };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const page = (title, body) => `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title></head><body>${body}</body></html>`;

function profile(slug) {
  const p = PEOPLE[slug];
  const degree = S.connected.includes(slug) ? '1st' : p.degree;
  const pending = S.pending.includes(slug);
  const connect = `<button aria-label="Invite ${esc(p.name)} to connect" data-connect>Connect</button>`;
  return page(p.name, `<main>
<section class="top-card"><h1>${esc(p.name)}</h1><span>· ${degree}</span><div>${esc(p.headline)}</div>
${pending ? `<button aria-label="Pending, click to withdraw invitation sent to ${esc(p.name)}">Pending</button>` : degree !== '1st' && !p.connectInMore ? connect : ''}
<button aria-label="Message ${esc(p.name)}" id="msg">Message</button>
<button aria-label="More actions" id="more">More</button>
<div role="menu" class="artdeco-dropdown__content" id="menu" hidden>${p.connectInMore && !pending && degree !== '1st' ? `<div role="button" tabindex="0" aria-label="Invite ${esc(p.name)} to connect" data-connect>Connect</div>` : ''}<div role="button" tabindex="0">Report</div></div>
</section>
<section><h2>People you may know</h2><button aria-label="Invite Someone Else to connect" id="decoy">Connect</button></section>
</main><div id="layer"></div>
<script>
const slug = ${JSON.stringify(slug)}, first = ${JSON.stringify(degree === '1st')}, thread = ${JSON.stringify(S.threads[slug] || [])};
const layer = document.getElementById('layer');
document.getElementById('more').onclick = () => { document.getElementById('menu').hidden = false; };
document.getElementById('decoy').onclick = () => fetch('/_decoy', { method: 'POST' });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { layer.innerHTML = ''; document.getElementById('menu').hidden = true; } });
document.querySelectorAll('[data-connect]').forEach((b) => b.onclick = () => {
  layer.innerHTML = '<div role="dialog"><h2>Add a note to your invitation?</h2><button id="addnote">Add a note</button><button id="nonote">Send without a note</button></div>';
  const send = async (note) => {
    const r = await (await fetch('/_invite', { method: 'POST', body: JSON.stringify({ slug, note }) })).json();
    if (r.limit) layer.innerHTML = '<div role="dialog"><h2>You\\u2019ve reached the weekly invitation limit</h2><button>Got it</button></div>';
    else location.reload();
  };
  document.getElementById('nonote').onclick = () => send('');
  document.getElementById('addnote').onclick = () => {
    layer.innerHTML = '<div role="dialog"><h2>Add a note</h2><textarea name="message" id="custom-message"></textarea><button aria-label="Send invitation" id="send" disabled>Send</button></div>';
    const ta = document.getElementById('custom-message'), sb = document.getElementById('send');
    ta.oninput = () => { sb.disabled = !ta.value.trim(); };
    sb.onclick = () => send(ta.value);
  };
});
document.getElementById('msg').onclick = () => {
  if (!first) return;
  layer.innerHTML = '<aside class="msg-overlay-conversation-bubble"><ul id="list"></ul><div role="textbox" contenteditable="true" id="box" style="min-height:40px;border:1px solid"></div><button id="msend" disabled>Send</button><button aria-label="Close your conversation with them" id="mclose">x</button></aside>';
  const list = document.getElementById('list'), box = document.getElementById('box'), sb = document.getElementById('msend');
  const add = (t) => { const li = document.createElement('li'); const p = document.createElement('p'); p.textContent = t; li.appendChild(p); list.appendChild(li); };
  thread.forEach(add);
  box.oninput = () => { sb.disabled = !box.innerText.trim(); };
  box.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sb.click(); } };
  sb.onclick = async () => { const text = box.innerText.trim(); await fetch('/_message', { method: 'POST', body: JSON.stringify({ slug, text }) }); add(text); box.innerText = ''; sb.disabled = true; };
  document.getElementById('mclose').onclick = () => { layer.innerHTML = ''; };
};
</script>`);
}

const COMPANY = page('HubSpot posts', `<main><div data-urn="urn:li:activity:1" class="feed-shared-update-v2"><p>We just launched new Sales Hub pricing.</p>
<button aria-label="5 reactions" id="reacts">5</button><button aria-label="2 comments" id="cbtn">2 comments</button>
<div class="comments-container" id="comments" hidden>
<article class="comments-comment-entity"><a href="https://www.linkedin.com/in/ana-ruiz?miniProfileUrn=x"><span>Ana Ruiz</span></a><span>• 2nd</span><div>VP Sales at Globex</div><p>How does pricing compare for a ten person team?</p></article>
<article class="comments-comment-entity"><a href="/company/some-agency/"><span>Some Agency</span></a><p>Great launch</p></article>
</div></div></main><div id="layer"></div><script>
document.getElementById('cbtn').onclick = () => { document.getElementById('comments').hidden = false; };
document.getElementById('reacts').onclick = () => { document.getElementById('layer').innerHTML = '<div role="dialog"><h2>Reactions</h2><ul><li><a href="/in/ben-ode-9"><div>Ben Ode</div><div>View Ben Ode\\u2019s profile</div></a><div>· 2nd</div><div>Founder @ Initech</div></li><li><a href="/company/some-page/">Some Page</a></li></ul></div>'; };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.getElementById('layer').innerHTML = ''; });
</script>`);
const SEARCH = page('Search', `<main><div data-urn="urn:li:activity:9" class="feed-shared-update-v2"><div class="update-components-actor"><a href="/in/cara-poster"><span>Cara Lin</span></a><div>Head of Growth at Vandelay</div></div><p>Does anyone have a cold email tool they actually like?</p></div></main>`);

const body = (req) => new Promise((res) => { let d = ''; req.on('data', (c) => { d += c; }); req.on('end', () => res(d)); });
http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${port}`);
  const send = (code, text, headers = {}) => { res.writeHead(code, { 'content-type': 'text/html; charset=utf-8', ...headers }); res.end(text); };
  const json = (o) => send(200, JSON.stringify(o), { 'content-type': 'application/json' });
  const signedIn = /li_at=1/.test(req.headers.cookie || '');
  const p = url.pathname;
  if (p === '/_state') return json(S);
  if (p === '/_accept') { S.connected.push(url.searchParams.get('slug')); S.pending = S.pending.filter((x) => x !== url.searchParams.get('slug')); return json({ ok: true }); }
  if (p === '/_reply') { (S.threads[url.searchParams.get('slug')] ||= []).push(url.searchParams.get('text')); return json({ ok: true }); }
  if (p === '/_decoy') { S.decoy = true; return json({}); }
  if (p === '/_invite') {
    const d = JSON.parse(await body(req));
    if (PEOPLE[d.slug]?.limit) return json({ limit: true });
    S.invites.push(d); S.pending.push(d.slug);
    return json({ ok: true });
  }
  if (p === '/_message') { const d = JSON.parse(await body(req)); S.messages.push(d); (S.threads[d.slug] ||= []).push(d.text); return json({ ok: true }); }
  if (p === '/login') return send(200, page('Sign in', '<main><form method="post" action="/login-submit"><input id="username" name="session_key"><input id="password" name="session_password" type="password"><button type="submit">Sign in</button></form></main>'));
  if (p === '/login-submit') {
    const f = new URLSearchParams(await body(req));
    S.logins++;
    if (f.get('session_key') === 'me@example.com' && f.get('session_password') === 'correct-horse')
      return send(302, '', { location: '/feed/', 'set-cookie': 'li_at=1; Path=/; Max-Age=31536000' });
    return send(302, '', { location: '/login' });
  }
  if (p.startsWith('/checkpoint')) return send(200, page('Security check', '<main><h1>Let’s do a quick security check</h1></main>'));
  if (!signedIn) return send(302, '', { location: '/login' });
  if (p === '/in/checkpoint-user/') return send(302, '', { location: '/checkpoint/challenge' });
  if (p === '/feed/') return send(200, page('Feed', '<main><section>Feed</section></main>'));
  if (p === '/mynetwork/invite-connect/connections/') return send(200, page('Connections', `<main><ul>${S.connected.map((s) => `<li><a href="/in/${s}/">${esc(PEOPLE[s]?.name || s)}</a></li>`).join('')}</ul></main>`));
  if (p === '/company/hubspot/posts/') return send(200, COMPANY);
  if (p === '/search/results/content/') return send(200, SEARCH);
  const m = /^\/in\/([^/]+)\/$/.exec(p);
  if (m && PEOPLE[m[1]]) return send(200, profile(m[1]));
  send(404, page('Not found', '<main><section>Not found</section></main>'));
}).listen(port, '127.0.0.1', () => console.log(`fake linkedin on ${port}`));
