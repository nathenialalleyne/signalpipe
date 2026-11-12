#!/usr/bin/env node
/**
 * Signalpipe LinkedIn agent. Runs on YOUR computer and drives YOUR Chrome, signed in as you.
 *
 *   node linkedin-agent.mjs run              sign in once in the window that opens, then it works on its own
 *   node linkedin-agent.mjs login            only sign in (by hand, or with LINKEDIN_EMAIL / LINKEDIN_PASSWORD)
 *   node linkedin-agent.mjs doctor <url>     show what the agent sees on a profile, without acting
 *
 * It asks the Signalpipe server for one unit of work at a time (send an invite, send a follow-up, check
 * who accepted, check for replies, read a watched page) and reports back. The server decides pacing and
 * caps. By default the agent starts your ordinary Chrome as a normal window and works in it at a person's
 * pace, moving the pointer and typing the way you would. It never tries to get past a LinkedIn verification
 * step (if LinkedIn asks you to verify, it stops and waits for you), and it never changes what the browser
 * reports about your device, your network or who you are.
 *
 * Your LinkedIn password, if you set one, is read from this machine's environment, typed into LinkedIn's
 * own sign-in page, and sent nowhere else.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const env = process.env;
const cfg = {
  server: (env.SIGNALPIPE_URL || '').replace(/\/$/, ''),
  token: env.SIGNALPIPE_TOKEN || '',
  base: (env.LINKEDIN_BASE || 'https://www.linkedin.com').replace(/\/$/, ''),
  profileDir: env.LINKEDIN_PROFILE_DIR || path.join(os.homedir(), '.signalpipe', 'chrome-profile'),
  errorDir: env.LINKEDIN_ERROR_DIR || path.join(os.homedir(), '.signalpipe', 'errors'),
  // attach: start your ordinary Chrome as a normal window and work in it (default).
  // launch: let the automation library start and own the browser (used by the tests).
  mode: env.LINKEDIN_MODE || 'attach',
  chromePath: env.LINKEDIN_CHROME_PATH || '',
  chromeArgs: (env.LINKEDIN_CHROME_ARGS || '').split(' ').filter(Boolean),
  channel: env.LINKEDIN_CHANNEL ?? 'chrome',      // launch mode only: '' uses Playwright's Chromium
  headless: env.LINKEDIN_HEADLESS === '1',
  allowNoNote: env.LINKEDIN_ALLOW_NO_NOTE !== '0', // free accounts get few noted invites a month
  email: env.LINKEDIN_EMAIL || '',
  password: env.LINKEDIN_PASSWORD || '',
  speed: Number(env.LINKEDIN_SPEED || 1),          // pause multiplier; tests set it near 0
};
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const opt = (name, d) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : d; };

class Blocked extends Error {}
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const dbg = (...a) => { if (env.LINKEDIN_DEBUG) console.log(new Date().toISOString().slice(11, 23), '  ·', ...a); };
const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms * cfg.speed)));
const pause = (a = 1200, b = 3200) => sleep(a + Math.random() * (b - a));
const slugOf = (url) => { const m = /\/in\/([^/?#]+)/.exec(url || ''); return m ? decodeURIComponent(m[1]) : ''; };
const profileUrl = (url) => `${cfg.base}/in/${encodeURIComponent(slugOf(url))}/`;

// Everything that depends on LinkedIn's page wording lives here, so a wording change is a one-line fix.
const T = {
  connect: /^(Invite .* to connect|Connect)$/i,
  pending: /^Pending/i,
  message: /^Message/i,
  more: /^More( actions)?$/i,
  addNote: /^Add a note/i,
  sendInvite: /^Send( invitation| now)?$/i,
  sendNoNote: /^Send without a note/i,
  send: /^Send$/i,
  limit: /weekly (invitation )?limit|reached the (weekly|monthly) limit|out of (personalized )?invit|too many/i,
  first: /(?:·|•|\b)\s*1st\b/,
  blockedPath: /^\/(login|checkpoint|authwall|uas\/|signup|m\/login)/,
};

async function api(method, route, body) {
  const r = await fetch(`${cfg.server}/api/agent/linkedin${route}`, {
    method, headers: { Authorization: `Bearer ${cfg.token}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`Signalpipe answered ${r.status} for ${route}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

function findChrome() {
  if (cfg.chromePath) return cfg.chromePath;
  const places = process.platform === 'darwin' ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    : process.platform === 'win32' ? [env.PROGRAMFILES, env['PROGRAMFILES(X86)'], env.LOCALAPPDATA].filter(Boolean)
      .map((d) => path.join(d, 'Google', 'Chrome', 'Application', 'chrome.exe'))
      : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium'];
  return places.find((f) => fs.existsSync(f)) || '';
}

const freePort = () => new Promise((resolve, reject) => {
  const srv = net.createServer();
  srv.on('error', reject);
  srv.listen(0, '127.0.0.1', () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
});

/** Returns { ctx, close }. The Chrome profile in cfg.profileDir holds your LinkedIn session between runs. */
async function launch(headless = cfg.headless) {
  const load = createRequire(import.meta.url);
  let pw;
  try { pw = load('playwright-core'); } catch { pw = load('playwright'); }
  fs.mkdirSync(cfg.profileDir, { recursive: true });
  if (cfg.mode === 'launch') {
    const ctx = await pw.chromium.launchPersistentContext(cfg.profileDir, {
      headless, viewport: { width: 1280, height: 900 }, ...(cfg.channel ? { channel: cfg.channel } : {}),
    });
    return { ctx, close: () => ctx.close() };
  }
  const exe = findChrome();
  if (!exe) throw new Error('Could not find Chrome. Set LINKEDIN_CHROME_PATH to its location.');
  const port = await freePort();  // reachable from this computer only
  // The three "disable" switches stop Chrome from slowing the page down when its window is behind others,
  // which otherwise makes the agent stall whenever you are working in another app.
  const child = spawn(exe, [`--remote-debugging-port=${port}`, `--user-data-dir=${cfg.profileDir}`, '--no-first-run',
    '--no-default-browser-check', '--window-size=1280,900', '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding', '--disable-background-timer-throttling',
    ...(headless ? ['--headless=new'] : []), ...cfg.chromeArgs, 'about:blank'],
  { stdio: 'ignore' });
  let up = false;
  for (let i = 0; i < 120 && !up && child.exitCode === null; i++) {
    try { up = (await fetch(`http://127.0.0.1:${port}/json/version`)).ok; } catch { await new Promise((r) => setTimeout(r, 250)); }
  }
  if (!up) { child.kill(); throw new Error('Chrome did not start. Is another agent already running with this profile?'); }
  const browser = await pw.chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  const close = async () => {
    try { await (await browser.newBrowserCDPSession()).send('Browser.close'); } catch { /* already gone */ }
    await Promise.race([new Promise((r) => child.once('exit', r)), new Promise((r) => setTimeout(r, 5000))]);
    if (child.exitCode === null) child.kill();
  };
  return { ctx: browser.contexts()[0], close };
}

/** The tab the agent works in. In attach mode Chrome may reopen with several tabs; a background tab is slowed
 *  down by Chrome, so open a fresh one, bring it to the front and close the leftovers. */
async function workPage(ctx) {
  const old = ctx.pages();
  if (cfg.mode === 'launch') return old[0] || ctx.newPage();
  const page = await ctx.newPage();
  for (const p of old) await p.close().catch(() => {});
  await page.bringToFront().catch(() => {});
  return page;
}

// Work the page the way a person does: move the pointer to the control, pause, press.
const pointer = { x: 300 + Math.random() * 300, y: 200 + Math.random() * 200 };
async function click(locator) {
  const page = locator.page();
  await locator.scrollIntoViewIfNeeded({ timeout: 5000 }).catch(() => {});
  const box = await locator.boundingBox().catch(() => null);
  if (!box) return locator.click();
  const px = box.width * (0.25 + Math.random() * 0.5), py = box.height * (0.3 + Math.random() * 0.4);
  const tx = box.x + px, ty = box.y + py;
  const bend = { x: (pointer.x + tx) / 2 + (Math.random() - 0.5) * 120, y: (pointer.y + ty) / 2 + (Math.random() - 0.5) * 120 };
  const steps = 12 + Math.floor(Math.random() * 14);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, u = 1 - t;
    await page.mouse.move(u * u * pointer.x + 2 * u * t * bend.x + t * t * tx, u * u * pointer.y + 2 * u * t * bend.y + t * t * ty);
    await sleep(5 + Math.random() * 14);
  }
  pointer.x = tx; pointer.y = ty;
  await sleep(90 + Math.random() * 220);
  const t0 = Date.now();
  try {
    return await locator.click({ position: { x: px, y: py }, delay: (40 + Math.random() * 80) * cfg.speed, timeout: 12000 });
  } finally { if (Date.now() - t0 > 2000) dbg(`slow click (${Date.now() - t0} ms) on`, String(locator).slice(0, 110)); }
}

/** Glance down the page and back, as you would before acting on a profile. */
async function look(page) {
  for (let i = 0, n = 1 + Math.floor(Math.random() * 3); i < n; i++) {
    await page.mouse.wheel(0, 250 + Math.random() * 500);
    await pause(700, 2200);
  }
  await page.mouse.wheel(0, -4000);
  await pause(500, 1200);
}

const onBlockedPage = (page) => T.blockedPath.test(new URL(page.url()).pathname);

/** Type the saved username and password into LinkedIn's sign-in form. Returns true once the feed loads. */
async function signIn(page, waitForHumanMs = 0) {
  if (!/\/login/.test(new URL(page.url()).pathname)) await page.goto(`${cfg.base}/login`, { waitUntil: 'domcontentloaded' });
  if (cfg.email && cfg.password) {
    const user = page.locator('#username, input[name="session_key"]').first();
    if (await user.count()) {
      await user.fill(cfg.email);
      await page.locator('#password, input[name="session_password"]').first().fill(cfg.password);
      await pause(400, 900);
      await click(page.locator('button[type="submit"]').first());
      await page.waitForLoadState('domcontentloaded').catch(() => {});
      await pause(1500, 2500);
    }
  }
  // A verification step (code, captcha, "is this you") is yours to complete; the agent only waits.
  const until = Date.now() + waitForHumanMs;
  while (onBlockedPage(page) && Date.now() < until) await new Promise((r) => setTimeout(r, 1500));
  return !onBlockedPage(page);
}

async function go(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await pause();
  if (onBlockedPage(page)) {
    const where = new URL(page.url()).pathname;
    // An expired session can be renewed with saved credentials; a verification challenge cannot.
    if (/login/.test(where) && cfg.email && cfg.password && await signIn(page)) {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await pause();
      if (!onBlockedPage(page)) return;
    }
    throw new Blocked(`LinkedIn showed ${new URL(page.url()).pathname} instead of the page`);
  }
}

const topCard = (page) => page.locator('main section').first();
const visibleButton = (scope, name) => scope.getByRole('button', { name }).filter({ visible: true });

async function profileState(page) {
  const card = topCard(page);
  await card.waitFor({ timeout: 20000 });
  const text = (await card.innerText()).replace(/\s+/g, ' ');
  const has = async (name) => (await visibleButton(card, name).count()) > 0;
  return {
    name: text.split(/ (?:·|•) |\n/)[0].trim().slice(0, 80),
    degree: T.first.test(text) ? 1 : /\b2nd\b/.test(text) ? 2 : /\b3rd/.test(text) ? 3 : 0,
    pending: await has(T.pending), connect: await has(T.connect), message: await has(T.message), more: await has(T.more),
  };
}

async function typeText(box, text) {
  const linesOf = String(text).split('\n');
  for (let i = 0; i < linesOf.length; i++) {
    if (linesOf[i]) await box.pressSequentially(linesOf[i], { delay: (20 + Math.random() * 45) * cfg.speed });
    if (i < linesOf.length - 1) await box.press('Shift+Enter');  // plain Enter would send the message
  }
}

async function closeDialogs(page) {
  for (let i = 0; i < 2; i++) {
    if (!(await page.getByRole('dialog').filter({ visible: true }).count())) return;
    await page.keyboard.press('Escape');
    await pause(300, 700);
  }
}

async function invite(page, task) {
  await go(page, profileUrl(task.profile_url));
  dbg('profile loaded');
  await look(page);
  const st = await profileState(page);
  dbg('state', JSON.stringify(st));
  if (st.degree === 1) return { outcome: 'already_connected' };
  if (st.pending) return { outcome: 'already_pending' };
  let btn = visibleButton(topCard(page), T.connect).first();
  if (!(await btn.count())) {
    if (!st.more) return { outcome: 'no_connect_button', detail: 'profile offers no Connect action' };
    await click(visibleButton(topCard(page), T.more).first());
    await pause(500, 1200);
    btn = page.locator('[role="menu"], .artdeco-dropdown__content').getByRole('button', { name: T.connect })
      .or(page.getByRole('menuitem', { name: T.connect })).filter({ visible: true }).first();
    if (!(await btn.count())) { await page.keyboard.press('Escape'); return { outcome: 'no_connect_button', detail: 'not in the More menu either' }; }
  }
  await click(btn);
  dbg('pressed connect');
  await pause(800, 1800);
  const dialog = page.getByRole('dialog').filter({ visible: true }).last();
  try { await dialog.waitFor({ timeout: 10000 }); } catch { return { outcome: 'error', detail: 'no invitation dialog appeared' }; }
  const said = await dialog.innerText();
  if (T.limit.test(said)) { await closeDialogs(page); return { outcome: 'limit', detail: said.slice(0, 120) }; }
  if (await dialog.locator('input[type="email"]').count()) { await closeDialogs(page); return { outcome: 'email_required', detail: 'LinkedIn wants their email address' }; }

  let withNote = false;
  if (task.note) {
    const add = visibleButton(dialog, T.addNote).first();
    if (await add.count()) {
      await click(add);
      await pause(500, 1200);
      const box = page.getByRole('dialog').filter({ visible: true }).last().locator('textarea').first();
      if (await box.count()) { await typeText(box, task.note); withNote = true; dbg('note typed'); await pause(600, 1400); }
    }
    if (!withNote && !cfg.allowNoNote) { await closeDialogs(page); return { outcome: 'note_unavailable', detail: 'could not add a note' }; }
  }
  const live = page.getByRole('dialog').filter({ visible: true }).last();
  const send = visibleButton(live, withNote ? T.sendInvite : T.sendNoNote).or(visibleButton(live, T.sendInvite)).first();
  if (!(await send.count()) || await send.isDisabled()) { await closeDialogs(page); return { outcome: 'error', detail: 'no usable Send button in the dialog' }; }
  await click(send);
  dbg('pressed send');
  // Wait for LinkedIn's answer instead of guessing a delay: either a limit warning, or the button turning to Pending.
  let pending = false;
  for (let i = 0; i < 40 && !pending; i++) {
    await new Promise((r) => setTimeout(r, 250));
    const open = page.getByRole('dialog').filter({ visible: true });
    if (await open.count().catch(() => 0)) {
      const t = await open.last().innerText().catch(() => '');
      if (T.limit.test(t)) { await closeDialogs(page); return { outcome: 'limit', detail: t.replace(/\s+/g, ' ').slice(0, 120) }; }
    } else {
      pending = (await visibleButton(topCard(page), T.pending).count().catch(() => 0)) > 0;
    }
  }
  await closeDialogs(page);
  if (!pending) return { outcome: 'unverified', detail: 'sent, but the profile does not show Pending' };
  return { outcome: withNote || !task.note ? 'sent' : 'sent_without_note' };
}

async function openThread(page, url) {
  await go(page, profileUrl(url));
  await look(page);
  const st = await profileState(page);
  if (st.degree !== 1 || !st.message) return null;  // never message someone who has not accepted
  await click(visibleButton(topCard(page), T.message).first());
  const box = page.locator('[role="textbox"][contenteditable="true"]').filter({ visible: true }).last();
  await box.waitFor({ timeout: 15000 });
  await pause(700, 1500);
  return box;
}

const threadTexts = (page) => page.evaluate(() => {
  const box = [...document.querySelectorAll('[role="textbox"][contenteditable="true"]')].pop();
  const scope = box?.closest('[class*="msg-overlay-conversation"], [class*="msg-convo"], [role="dialog"], section, aside') || document.body;
  return [...scope.querySelectorAll('[class*="msg-s-event-listitem__body"], [class*="event-listitem"] p, li p')]
    .map((e) => e.innerText.trim()).filter(Boolean);
});

async function closeThread(page) {
  const close = page.getByRole('button', { name: /^Close your (conversation|draft)/i }).filter({ visible: true });
  if (await close.count()) await click(close.first()).catch((e) => dbg('close thread failed:', e.message.split('\n')[0]));
}

async function message(page, task) {
  const box = await openThread(page, task.profile_url);
  if (!box) return { outcome: 'not_connected' };
  dbg('thread open');
  await click(box);
  await typeText(box, task.text);
  dbg('message typed');
  await pause(600, 1500);
  const send = page.getByRole('button', { name: T.send }).filter({ visible: true }).last();
  if (!(await send.count()) || await send.isDisabled()) { await closeThread(page); return { outcome: 'error', detail: 'Send button missing or disabled' }; }
  await click(send);
  await pause(1200, 2500);
  const left = (await box.innerText().catch(() => '')).trim();
  await closeThread(page);
  return { outcome: left ? 'unverified' : 'sent', detail: left ? 'text still in the box after Send' : '' };
}

async function checkConnections(page, task) {
  await go(page, `${cfg.base}/mynetwork/invite-connect/connections/`);
  await page.mouse.wheel(0, 2500);
  await pause(800, 1600);
  const slugs = new Set(await page.evaluate(() => [...document.querySelectorAll('main a[href*="/in/"]')]
    .map((a) => (/\/in\/([^/?#]+)/.exec(a.getAttribute('href')) || [])[1]).filter(Boolean)
    .map((s) => decodeURIComponent(s).toLowerCase())));
  const accepted = task.pending.filter((p) => slugs.has(p.slug)).map((p) => p.message_id);
  // The list only shows recent connections under their public name; look at a few of the rest directly.
  const rest = task.pending.filter((p) => !slugs.has(p.slug)).sort(() => Math.random() - 0.5).slice(0, 3);
  for (const p of rest) {
    try {
      await go(page, `${cfg.base}/in/${encodeURIComponent(p.slug)}/`);
      if ((await profileState(page)).degree === 1) accepted.push(p.message_id);
    } catch (e) { if (e instanceof Blocked) throw e; }
  }
  return { accepted };
}

async function checkReplies(page, task) {
  const replies = [];
  for (const t of task.threads) {
    try {
      const box = await openThread(page, t.profile_url);
      if (!box) continue;
      const texts = await threadTexts(page);
      const key = (t.sent_text || '').slice(0, 40);
      const at = key ? texts.map((x) => x.startsWith(key)).lastIndexOf(true) : -1;
      const theirs = at >= 0 ? texts.slice(at + 1) : [];  // only what came after our own message
      if (theirs.length) replies.push({ message_id: t.message_id, lead_id: t.lead_id, contact_id: t.contact_id, text: theirs.join('\n') });
      await closeThread(page);
      await pause();
    } catch (e) { if (e instanceof Blocked) throw e; log('could not read thread for', t.name, '-', e.message.slice(0, 80)); }
  }
  return { replies };
}

/** Names, headlines and profile links found under the elements matching `sel`. Runs in the page. */
const harvest = (page, sel) => page.evaluate((selector) => {
  const drop = /^(View .*profile|.*degree connection|1st|2nd|3rd\+?|Author|Following|Follow|Connect|Message|·.*|•.*|\d+[smhdw]o?|Like|Reply|Premium.*)$/i;
  const out = new Map();
  for (const scope of document.querySelectorAll(selector)) {
    for (const a of scope.querySelectorAll('a[href*="/in/"]')) {
      const slug = (/\/in\/([^/?#]+)/.exec(a.getAttribute('href')) || [])[1];
      if (!slug || out.has(slug)) continue;
      const box = a.closest('li, article, [class*="comment"], [class*="actor"], [class*="entity"]') || a.parentElement;
      const tidy = (x) => x.replace(/\s*[·•]\s*(1st|2nd|3rd\+?)\b.*$/i, '').trim();  // "Ana Ruiz • 2nd" -> "Ana Ruiz"
      const rowsOf = (el) => (el?.innerText || '').split('\n').map((x) => tidy(x.trim())).filter((x) => x && !drop.test(x));
      const name = rowsOf(a)[0] || rowsOf(box)[0] || '';
      if (name.length < 3 || name.length > 60) continue;
      const rest = rowsOf(box).filter((x) => x !== name && !x.startsWith(name));
      out.set(slug, { slug, name, headline: (rest.find((x) => x.length > 5) || '').slice(0, 160), text: rest.slice(1).join(' ').slice(0, 300) });
    }
  }
  return [...out.values()];
}, sel);

const POSTS = '[data-urn^="urn:li:activity"], div.feed-shared-update-v2';

async function collect(page, task) {
  const people = [];
  const add = (p, extra) => people.push({ name: p.name, headline: p.headline, profile_url: `https://www.linkedin.com/in/${p.slug}`, ...extra });
  for (const entry of task.pages || []) {
    try {
      const company = /\/(company|school)\/([^/?#]+)/.exec(entry);
      const person = slugOf(entry);
      const watched = company ? company[2] : person || entry;
      await go(page, company ? `${cfg.base}/company/${company[2]}/posts/?feedView=all` : `${cfg.base}/in/${encodeURIComponent(person)}/recent-activity/all/`);
      await page.mouse.wheel(0, 2200);
      await pause();
      const posts = page.locator(POSTS);
      for (let i = 0; i < Math.min(await posts.count(), 3); i++) {
        const post = posts.nth(i);
        const postText = (await post.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 300);
        const expand = post.getByRole('button', { name: /\d[\d,]* comments?/i }).filter({ visible: true }).first();
        if (await expand.count()) { await click(expand).catch(() => {}); await pause(900, 1800); }
        await post.evaluate((el, n) => el.setAttribute('data-sp-post', n), String(i));
        for (const c of await harvest(page, `[data-sp-post="${i}"] [class*="comment"]`)) add(c, { action: 'comment', watched, post_text: postText, comment: c.text });
        const reacts = post.getByRole('button', { name: /reaction/i }).filter({ visible: true }).first();
        if (await reacts.count()) {
          await click(reacts).catch(() => {});
          await pause(900, 1800);
          if (await page.getByRole('dialog').filter({ visible: true }).count()) {
            for (const r of await harvest(page, '[role="dialog"]')) add(r, { action: 'reaction', watched, post_text: postText });
            await closeDialogs(page);
          }
        }
      }
    } catch (e) { if (e instanceof Blocked) throw e; log('could not read', entry, '-', e.message.slice(0, 80)); }
  }
  for (const kw of task.keywords || []) {
    try {
      await go(page, `${cfg.base}/search/results/content/?keywords=${encodeURIComponent(kw)}&datePosted=%22past-week%22&sortBy=%22date_posted%22`);
      await page.mouse.wheel(0, 1800);
      await pause();
      const posts = page.locator(POSTS);
      for (let i = 0; i < Math.min(await posts.count(), 10); i++) {
        await posts.nth(i).evaluate((el, n) => el.setAttribute('data-sp-hit', n), String(i));
        const postText = (await posts.nth(i).innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 300);
        const [author] = await harvest(page, `[data-sp-hit="${i}"]`);
        if (author) add(author, { action: 'post', term: kw, post_text: postText });
      }
    } catch (e) { if (e instanceof Blocked) throw e; log('could not search', kw, '-', e.message.slice(0, 80)); }
  }
  const seen = new Set();
  return { people: people.filter((p) => !seen.has(p.profile_url + p.action) && seen.add(p.profile_url + p.action)).slice(0, 300) };
}

const HANDLERS = { invite, message, check_connections: checkConnections, check_replies: checkReplies, collect };

async function snapshot(page, label) {
  try {
    fs.mkdirSync(cfg.errorDir, { recursive: true });
    const file = path.join(cfg.errorDir, `${new Date().toISOString().replace(/[:.]/g, '-')}-${label}.png`);
    await page.screenshot({ path: file });
    return file;
  } catch { return ''; }
}

async function ensureSignedIn(page, interactive) {
  await page.goto(`${cfg.base}/feed/`, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await pause(800, 1500);
  if (!onBlockedPage(page)) return true;
  return signIn(page, interactive ? 10 * 60 * 1000 : 0);
}

async function cmdLogin() {
  const { ctx, close } = await launch(cfg.headless);  // headless sign-in only makes sense with a saved username and password
  const page = await workPage(ctx);
  log(cfg.email ? 'Signing in with the saved username and password.' : 'Sign in to LinkedIn in the window that opened.');
  log('If LinkedIn asks for a code or a check, complete it there. Waiting up to 10 minutes.');
  const ok = await ensureSignedIn(page, true);
  if (ok && cfg.server) await api('POST', '/result', { kind: 'ready' }).catch((e) => log('signed in, but could not tell Signalpipe:', e.message));
  log(ok ? 'Signed in. You can now start the agent with: node linkedin-agent.mjs run' : 'Still not signed in; nothing was changed.');
  await close();
  process.exit(ok ? 0 : 2);
}

async function cmdDoctor(url) {
  const { ctx, close } = await launch();
  const page = await workPage(ctx);
  try {
    await go(page, profileUrl(url));
    const flagged = await page.evaluate(() => navigator.webdriver);
    console.log(JSON.stringify({ url: page.url(), mode: cfg.mode, browser_says_automated: flagged, ...(await profileState(page)), screenshot: await snapshot(page, 'doctor') }, null, 2));
  } catch (e) { console.log(JSON.stringify({ error: e.message, url: page.url(), screenshot: await snapshot(page, 'doctor') }, null, 2)); }
  await close();
}

async function cmdRun() {
  if (!cfg.server || !cfg.token) { console.error('Set SIGNALPIPE_URL and SIGNALPIPE_TOKEN first.'); process.exit(1); }
  const maxWork = Number(opt('--max-work', Infinity));
  const { ctx, close } = await launch();
  const page = await workPage(ctx);
  const stop = async (code) => { await close().catch(() => {}); process.exit(code); };
  process.on('SIGINT', () => stop(0));
  // First run, or the session ended: the window is on LinkedIn's sign-in page. Sign in there once and the
  // agent carries on by itself; the session is kept in its own Chrome profile for next time.
  const canWait = !cfg.headless && !flag('--no-wait');
  if (canWait) log('If the window shows LinkedIn\'s sign-in page, sign in there. Waiting up to 10 minutes.');
  if (!(await ensureSignedIn(page, canWait))) {
    await api('POST', '/result', { kind: 'blocked', reason: 'not signed in to LinkedIn' });
    log('Not signed in. Start the agent again and sign in in the window that opens.');
    return stop(2);
  }
  await api('POST', '/result', { kind: 'ready' });
  log('Signed in. Asking Signalpipe for work.');
  let done = 0;
  while (done < maxWork) {
    const work = await api('GET', '/work');
    if (work.kind === 'idle') {
      if (flag('--exit-on-idle')) break;
      log(`idle ${work.seconds}s: ${work.reason}`);
      await new Promise((r) => setTimeout(r, Math.min(work.seconds, 900) * 1000));
      continue;
    }
    let result;
    try {
      result = await HANDLERS[work.kind](page, work);
    } catch (e) {
      if (e instanceof Blocked) {
        await api('POST', '/result', { kind: 'blocked', reason: e.message });
        log('Stopped:', e.message, '\nRun: node linkedin-agent.mjs login');
        return stop(2);
      }
      result = { outcome: 'error', detail: `${e.message.slice(0, 160)} (screenshot: ${await snapshot(page, work.kind)})` };
      await closeDialogs(page).catch(() => {});
    }
    const reply = await api('POST', '/result', { kind: work.kind, message_id: work.message_id, play_id: work.play_id, ...result });
    log(work.kind, work.name || '', JSON.stringify(result).slice(0, 140), '->', JSON.stringify(reply));
    done++;
    await sleep(15000 + Math.random() * 45000);
  }
  await stop(0);
}

const cmd = args[0];
if (cmd === 'login') await cmdLogin();
else if (cmd === 'run') await cmdRun();
else if (cmd === 'doctor' && args[1]) await cmdDoctor(args[1]);
else { console.log('Usage: node linkedin-agent.mjs login | run [--max-work N] [--exit-on-idle] | doctor <profile url>'); process.exit(1); }
