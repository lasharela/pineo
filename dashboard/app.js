/* Pineo dashboard: live view of the hub over one WebSocket. No build step. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const S = {
    bugs: [], openBugId: null, session: null, sessions: [], band: {}, appContext: null, assists: [], clients: {},
    kb: {}, selectedId: null, interim: '', idleLines: [], flash: null, clockOffset: 0, wsUp: false,
  };
  const now = () => Date.now() + S.clockOffset;

  // ------------------------------------------------------------ formatting
  const pad = (n) => String(n).padStart(2, '0');
  const mmss = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${pad(s % 60)}`; };
  const clock = (ts) => { const d = new Date(ts); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const ago = (ts) => {
    const s = Math.floor((now() - ts) / 1000);
    if (s < 5) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return clock(ts);
  };
  const titleCase = (s) => String(s || '').replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  const bugById = (id) => S.bugs.find((b) => b.id === id);
  const spoken = (bug) => (bug.transcript || []).map((t) => t.text).join(' ');
  function bugTitle(bug) {
    if (bug.title) return { text: bug.title, pending: false };
    const words = spoken(bug).split(/\s+/).filter(Boolean);
    if (words.length) return { text: words.slice(0, 9).join(' ') + (words.length > 9 ? '…' : ''), pending: true };
    return { text: bug.status === 'open' ? 'Listening…' : 'No description', pending: true };
  }
  const statusTag = (bug) => {
    const label = { open: 'Reporting', submitted: 'Sent', cancelled: 'Cancelled' }[bug.status] || bug.status;
    return `<span class="tag ${esc(bug.status)}"><i></i>${esc(label)}</span>`;
  };
  const kindTag = (bug) => bug.kind ? `<span class="tag kind-${esc(bug.kind)}">${esc(bug.kind)}</span>` : '';
  const sevTag = (bug) => bug.severity ? `<span class="tag sev-${esc(bug.severity)}">${esc(bug.severity)}</span>` : '';
  const FIX = { queued: 'Fix queued', working: 'Agent fixing…', done: 'Fixed live', failed: 'Not changed' };
  const fixTag = (bug) => FIX[bug.fix?.state] ? `<span class="tag fix-${esc(bug.fix.state)}"><i></i>${esc(FIX[bug.fix.state])}</span>` : '';
  // a submitted ticket leads with what it is; open and cancelled ones lead with their status
  const tags = (bug) => (bug.status === 'submitted' && bug.kind ? kindTag(bug) : statusTag(bug)) + sevTag(bug) + fixTag(bug);
  const elName = (id) => S.kb[id]?.label || id;
  function elementChip(id) {
    const el = S.kb[id];
    return `<span class="chip" title="${esc(id)}">${esc(el?.label || id)}${el?.screen ? `<small>${esc(titleCase(el.screen))}</small>` : ''}</span>`;
  }

  // ------------------------------------------------------------ header
  function renderHeader() {
    $('pill-hub').hidden = S.wsUp;
    const band = S.band || {};
    // hardware status only: the microphone is always on by design, so this must not read as "session open"
    $('pill-band').classList.toggle('up', !!band.connected);
    const sttDown = band.connected && band.stt === 'reconnecting';
    // link: what the band process is doing about the wristband; without a process there is nothing to control
    const present = (S.clients.band || 0) > 0 || !!band.connected;
    const link = !present ? 'none' : band.link || (band.connected ? 'connected' : 'disconnected');
    // the chip carries only what the band is doing; whether it is connected lives on the button's dot
    $('band-text').textContent = sttDown ? 'speech reconnecting…' : band.connected && band.listening ? 'mic on' : 'mic off';
    $('pill-band').hidden = link === 'none';
    renderBandButton(link);
    $('pill-band').classList.toggle('warn', sttDown);
    const hasBattery = band.connected && typeof band.battery === 'number';
    $('band-battery').hidden = !hasBattery;
    if (hasBattery) {
      const pct = Math.round(band.battery <= 1 ? band.battery * 100 : band.battery);
      $('band-battery-fill').style.width = `${Math.max(6, Math.min(100, pct))}%`;
      $('band-battery-text').textContent = pct < 15 ? `${pct} % · charge the band` : `${pct} %`;
      $('band-battery').classList.toggle('low', pct < 15);
    }
    $('pill-band').classList.toggle('lowbat', hasBattery && Math.round(band.battery <= 1 ? band.battery * 100 : band.battery) < 15);
    const phoneOn = (S.clients.app || 0) > 0;
    $('pill-phone').classList.toggle('up', phoneOn);
    const screen = S.appContext?.screen;
    $('phone-text').textContent = phoneOn ? (screen ? `Phone on ${titleCase(screen)}` : 'Phone connected') : 'Phone offline';
  }

  // ------------------------------------------------------------ band connect / disconnect button
  const B = { link: 'none', busy: 0, armed: 0, note: '' };
  function renderBandButton(link) {
    if (link) { if (link !== B.link) { B.busy = 0; B.note = ''; } B.link = link; }
    const btn = $('band-toggle');
    const busy = Date.now() - B.busy < 4000;
    const armed = Date.now() - B.armed < 4000;
    const searching = B.link === 'searching' || B.link === 'connecting';
    btn.disabled = B.link === 'none' || searching || busy;
    $('band-toggle-label').textContent = B.link === 'none' ? 'Band process not running' : searching ? 'Connecting…' : busy ? 'Working…'
      : B.link === 'connected' ? (armed ? 'End session and disconnect?' : 'Disconnect') : 'Connect';
    btn.dataset.link = B.link === 'none' ? 'none' : searching ? 'connecting' : B.link === 'connected' ? 'connected' : 'disconnected';
    btn.classList.toggle('danger', armed && B.link === 'connected');
    const hint = B.note || (searching ? (S.band.hint || 'Double-tap the band so it blinks blue') : '');
    $('band-hint').textContent = hint;
    $('band-hint').hidden = !hint;
  }
  $('band-toggle').addEventListener('click', async () => {
    const action = B.link === 'connected' ? 'disconnect' : 'connect';
    if (action === 'disconnect' && S.session && Date.now() - B.armed >= 4000) {
      B.armed = Date.now(); // a live session: ask once more instead of a blocking dialog
      renderBandButton();
      setTimeout(renderBandButton, 4100);
      return;
    }
    B.armed = 0; B.busy = Date.now(); B.note = '';
    renderBandButton();
    try {
      const res = await fetch('/api/band/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
      const data = await res.json();
      if (!res.ok) { B.note = data.error || 'The band process did not answer.'; B.busy = 0; }
    } catch (e) { B.note = 'The hub did not answer.'; B.busy = 0; }
    renderBandButton();
    setTimeout(renderBandButton, 4100);
  });

  // ------------------------------------------------------------ live strip
  let gestureTimer = 0;
  function showGesture(msg) {
    const what = { double: 'Double rotation', single: 'Single rotation', next: '“Next issue”', open: 'Open', submit: 'Submit', cancel: 'Cancel' }[msg.name] || msg.name;
    const effect = msg.accepted === false || !msg.action
      ? (msg.name === 'single' && !msg.action ? 'ignored, no session' : 'ignored')
      : { 'session.start': 'session started', 'session.end': msg.closing ? 'closing the ticket, then ending the session' : 'session ended',
        'ticket.open': 'ticket opened', 'ticket.submit': 'closing the ticket', 'ticket.cancel': 'ticket cancelled',
        'ticket.next': 'next ticket' }[msg.action] || msg.action;
    const chip = $('gesture-chip');
    chip.textContent = `${what} → ${effect}, ${clock(msg.ts || now())}`;
    chip.classList.toggle('ignored', msg.accepted === false || !msg.action);
    chip.classList.add('show', 'fresh');
    clearTimeout(gestureTimer);
    gestureTimer = setTimeout(() => chip.classList.remove('fresh'), 2600);
  }

  function renderLive() {
    const open = bugById(S.openBugId);
    const live = $('live');
    const flash = S.flash && now() - S.flash.at < (S.flash.kind === 'session-ended' ? 5000 : 3500) ? S.flash : null;
    const session = S.session;
    const count = session ? S.bugs.filter((b) => b.sessionId === session.id && b.status === 'submitted').length : 0;
    const secs = (ms) => `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
    // the coding agent: the newest ticket it is working on, or one it finished in the last 8 s
    const fixFlash = S.fixFlash && now() - S.fixFlash.at < 8000 ? bugById(S.fixFlash.id) : null;
    const working = S.bugs.find((b) => b.status === 'submitted' && ['queued', 'working'].includes(b.fix?.state));
    let mode, label, sub, timer = '';
    if (open) {
      mode = 'recording';
      label = open.closing ? 'Closing…' : 'Reporting…';
      timer = mmss(now() - open.openedAt);
      sub = open.closing ? 'Catching the last words…' : 'Say it and tap what you mean. It closes by itself when you are done.';
    } else if (fixFlash && (!flash || S.fixFlash.at >= flash.at)) {
      const fix = fixFlash.fix || {};
      if (fix.state === 'done') { mode = 'done'; label = fix.ms ? `Fixed live · ${secs(fix.ms)}` : 'Fixed live'; }
      else { mode = 'failed'; label = 'Couldn’t change that'; }
      sub = fix.summary || bugTitle(fixFlash).text;
    } else if (flash) {
      mode = flash.kind === 'submitted' ? 'done' : flash.kind === 'session-ended' ? 'ended' : 'idle';
      label = { submitted: 'Ticket sent', 'session-ended': `Session ended · ${flash.tickets || 0} sent` }[flash.kind]
        || (flash.reason === 'empty' ? 'Nothing captured' : 'Ticket cancelled');
      sub = flash.kind === 'session-ended' ? 'Rotate twice to start a new session.'
        : flash.kind === 'submitted' ? 'Handed to the coding agent.'
        : flash.reason === 'empty' ? 'Closed without a ticket: nothing was said or pointed at.'
        : session ? `Session live · ${count} sent` : '';
    } else if (working) {
      mode = 'fixing';
      label = working.fix.state === 'working' ? 'Agent fixing…' : 'Agent queued…';
      timer = working.fix.startedAt ? mmss(now() - working.fix.startedAt) : '';
      sub = working.fix.summary || bugTitle(working).text;
    } else if (session) {
      mode = 'session';
      label = 'Session live';
      timer = mmss(now() - session.startedAt);
      sub = `${count} sent · mic on · rotate once to report, twice to end`;
    } else {
      mode = 'idle';
      label = 'No session';
      sub = 'Rotate twice to start: the mic turns on with the session.';
    }
    for (const m of ['recording', 'done', 'session', 'ended', 'fixing', 'failed']) live.classList.toggle(m, mode === m);
    $('state-label').textContent = label;
    $('state-timer').textContent = timer;
    $('state-sub').textContent = sub;

    S.idleLines = S.idleLines.filter((l) => now() - l.at < 12000);
    const finals = open ? (open.transcript || []).map((t) => t.text) : S.idleLines.map((l) => l.text);
    let html = finals.map((t) => `<span class="final">${esc(t)} </span>`).join('');
    if (S.interim) html += `<span class="interim">${esc(S.interim)}</span>`;
    if (!html) html = `<span class="hint">${open ? 'Describe it out loud…' : session ? 'Listening. Report something, or ask “where is…?”' : 'The mic is off. Speech appears here during a session.'}</span>`;
    if ($('live-text').innerHTML !== html) $('live-text').innerHTML = html;
    const box = $('live-transcript'); // fade the top only when older lines are actually cut off
    box.classList.toggle('clipped', $('live-text').offsetHeight > box.clientHeight + 2);
  }

  // ------------------------------------------------------------ bug list
  const bugEls = new Map();
  function bugCardHtml(bug) {
    const t = bugTitle(bug);
    const screen = bug.screen ? `<span>${esc(titleCase(bug.screen))}</span><span class="sep">·</span>` : '';
    return `<div class="bug-top">${tags(bug)}</div>
      <div class="bug-title${t.pending ? ' pending' : ''}">${esc(t.text)}</div>
      <div class="bug-meta">${screen}<span data-ago="${bug.openedAt}">${esc(ago(bug.openedAt))}</span></div>`;
  }
  function renderList(animateId) {
    const host = $('bug-list');
    const shown = S.bugs.filter((b) => b.status !== 'cancelled'); // cancelled reports are not tickets
    const dropped = S.bugs.length - shown.length;
    const ids = new Set(shown.map((b) => b.id));
    for (const [id, el] of bugEls) if (!ids.has(id)) { el.remove(); bugEls.delete(id); }
    host.querySelectorAll('.empty, .dropped, .group').forEach((el) => el.remove());
    if (!shown.length) host.insertAdjacentHTML('afterbegin', `<div class="empty">${S.session ? 'No tickets yet. Rotate once to report one.' : 'No tickets yet. Rotate twice to start a session.'}</div>`);
    let before = host.firstChild;
    for (const bug of shown) { // newest first
      let el = bugEls.get(bug.id);
      if (!el) {
        el = document.createElement('button');
        el.type = 'button';
        el.dataset.id = bug.id;
        el.addEventListener('click', () => select(bug.id));
        bugEls.set(bug.id, el);
      }
      const html = bugCardHtml(bug);
      if (el._html !== html) { el.innerHTML = html; el._html = html; }
      el.className = `bug ${bug.status}${bug.id === S.selectedId ? ' selected' : ''}${el.classList.contains('enter') ? ' enter' : ''}`;
      if (bug.id === animateId) {
        el.classList.remove('enter');
        void el.offsetWidth; // restart the animation
        el.classList.add('enter');
      }
      if (el !== before) host.insertBefore(el, before); else before = before.nextSibling;
    }
    // group tickets under their session: a divider before the first ticket of each one
    const liveId = S.session?.id;
    const groupLabel = (id, n) => {
      const tickets = `${n} sent`;
      if (id && id === liveId) return `<i></i>Session live · since ${clock(S.session.startedAt).slice(0, 5)} · ${tickets}`;
      const ended = S.sessions.find((x) => x.id === id);
      if (ended) return `Session ended ${clock(ended.endedAt).slice(0, 5)} · ${tickets}`;
      return id ? `Session · ${tickets}` : `Earlier · ${tickets}`;
    };
    const divider = (id, n) => { const el = document.createElement('div'); el.className = `group${id && id === liveId ? ' now' : ''}`; el.innerHTML = groupLabel(id, n); return el; };
    let last;
    shown.forEach((bug, k) => {
      const id = bug.sessionId || null;
      if (k > 0 && id === last) return;
      last = id;
      const n = shown.filter((b) => (b.sessionId || null) === id && b.status === 'submitted').length;
      host.insertBefore(divider(id, n), bugEls.get(bug.id));
    });
    if (liveId && !shown.some((b) => b.sessionId === liveId)) host.insertBefore(divider(liveId, 0), host.firstChild);
    if (dropped) host.insertAdjacentHTML('beforeend', `<div class="dropped">${dropped} closed with nothing captured or cancelled</div>`);
    const sent = shown.filter((b) => b.status === 'submitted').length;
    $('bug-count').textContent = shown.length ? `${sent} sent` : '';
  }

  // ------------------------------------------------------------ bug detail
  function renderDetail() {
    const bug = bugById(S.selectedId);
    $('detail').hidden = !bug;
    $('detail-empty').hidden = !!bug;
    if (!bug) { syncPointing(null); return; }
    const t = bugTitle(bug);
    const meta = [bug.screen && `Screen: ${titleCase(bug.screen)}`, `Opened ${clock(bug.openedAt)}`,
      bug.submittedAt ? `${mmss(bug.submittedAt - bug.openedAt)} long` : null].filter(Boolean).join('  ·  ');
    let summary;
    if (bug.summary) summary = `<p class="summary">${esc(bug.summary)}</p>`;
    else if (bug.status === 'open') summary = '<p class="summary pending">The write-up appears after the ticket is sent.</p>';
    else if (bug.status === 'submitted' && now() - bug.submittedAt < 45000) summary = '<p class="summary shimmer">Writing up the ticket…</p>';
    else summary = '<p class="summary pending">No write-up for this ticket.</p>';
    const elements = (bug.elementIds || []).length
      ? `<div class="chips">${bug.elementIds.map(elementChip).join('')}</div>`
      : '<div class="chips"><span class="chip none">None identified yet</span></div>';
    const steps = (bug.steps || []).length
      ? `<h3>Steps</h3><ol class="steps">${bug.steps.map((x) => `<li>${esc(x)}</li>`).join('')}</ol>` : '';
    const fix = bug.fix || {};
    let fixHtml = '';
    if (FIX[fix.state]) {
      const secs = (ms) => `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
      const running = fix.startedAt ? ` ${mmss(now() - fix.startedAt)}` : '';
      const head = {
        queued: `Waiting for the coding agent…${running}`,
        working: `Agent fixing…${running}`,
        done: fix.ms ? `Done in ${secs(fix.ms)}` : 'Done',
        failed: 'Couldn’t change that',
      }[fix.state];
      const took = '';
      fixHtml = `<div class="fix fix-${esc(fix.state)}"><div class="fix-head"><i></i>${esc(head + took)}</div>
        ${fix.summary ? `<p>${esc(fix.summary)}</p>` : ''}
        ${(fix.files || []).length ? `<div class="files">${fix.files.map((f) => `<code>${esc(f)}</code>`).join('')}</div>` : ''}</div>`;
    }
    const rows = [
      ...(bug.transcript || []).map((l) => ({ at: l.at, html: `<span>${esc(l.text)}${l.amended ? ' <em class="amend">added after closing</em>' : ''}</span>` })),
      ...closedRow(bug),
      ...(bug.interactions || []).map((i) => ({ at: i.at, html: `<span class="act${i.selected === false ? ' undone' : ''}"><b>${esc(actionVerb(i))}</b> ${esc(actionTarget(i))}</span>` })),
    ].sort((a, b) => a.at - b.at);
    const lines = rows.length
      ? `<ol class="lines">${rows.map((r) => `<li><time>+${mmss(r.at - bug.openedAt)}</time>${r.html}</li>`).join('')}</ol>`
      : `<div class="empty">${bug.status === 'open' ? 'Waiting for speech, taps or circles…' : 'Nothing was said or pointed at.'}</div>`;
    const said = (bug.transcript || []).length
      ? `<blockquote class="said">“${bug.transcript.map((l) => `${esc(l.text)}${l.amended ? ' <em class="amend">added after closing</em>' : ''}`).join(' ')}”</blockquote>`
      : `<p class="said none">${bug.status === 'open' ? 'Waiting for speech…' : 'Nothing was said.'}</p>`;
    const html = `<div class="tags">${tags(bug)}</div>
      <h1 class="${t.pending ? 'pending' : ''}">${esc(t.text)}</h1>
      <div class="meta">${esc(meta)}</div>
      ${fixHtml}
      <h3>What was said</h3>${said}
      <h3>Summary</h3>${summary}
      <h3>Elements involved</h3>${elements}
      ${steps}
      <h3>Timeline</h3>${lines}`;
    const host = $('detail-text');
    if (host._html !== html) { host.innerHTML = html; host._html = html; }
    syncPointing(bug);
  }

  function closedRow(bug) {
    if (bug.status === 'open' || !bug.closedAt) return [];
    const how = { gesture: 'by wrist rotation', idle: 'automatically after a pause', next: 'by “next issue”',
      'session-end': 'when the session ended', complete: 'by the AI: the description was complete' }[bug.closedBy];
    if (!how && bug.cancelReason !== 'gesture') return [];
    const text = bug.cancelReason === 'gesture' ? 'Cancelled' : `Closed ${how}`;
    return [{ at: bug.closedAt, html: `<span class="closed">${esc(text)}${bug.completeReason ? ` (${esc(bug.completeReason)})` : ''}</span>` }];
  }

  function select(id) {
    S.selectedId = id;
    renderList();
    renderDetail();
  }

  // ------------------------------------------------------------ what was pointed at (taps and drawn circles)
  const actionVerb = (i) => (i.kind === 'tap' && typeof i.selected === 'boolean' ? (i.selected ? 'Selected' : 'Deselected')
    : { tap: 'Tapped', circle: 'Circled', screen: 'Opened', scroll: 'Scrolled', input: 'Typed in' }[i.kind] || i.kind);
  function actionTarget(i) {
    if (i.kind === 'screen') return `${titleCase(i.screen)} screen`;
    if (i.kind === 'circle' && (i.elementIds || []).length) return i.elementIds.map(elName).join(', ');
    if (i.kind === 'circle') return 'an area';
    return i.label || (i.elementId ? elName(i.elementId) : i.kind === 'scroll' ? `${titleCase(i.screen)}` : 'the screen');
  }
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  function viewportOf(bug) {
    const dims = (v) => (v && (num(v.width) || num(v.w)) && (num(v.height) || num(v.h))
      ? { vw: num(v.width) || num(v.w), vh: num(v.height) || num(v.h) } : null);
    // the phone's own viewport when known; otherwise the owner's iPhone (402×812)
    return dims(bug.contextAtOpen?.viewport) || dims(S.appContext?.viewport) || { vw: 402, vh: 812 };
  }
  function rectOf(i, vw, vh) {
    const r = i.rect;
    if (!r || [r.x, r.y, r.w, r.h].some((v) => num(v) === null) || r.w <= 0 || r.h <= 0) return null;
    const frac = Math.max(r.x + r.w, r.y + r.h) <= 1.5; // fractions of the viewport, or CSS pixels
    return frac ? { x: r.x * vw, y: r.y * vh, w: r.w * vw, h: r.h * vh } : r;
  }
  const P = { key: '' };

  function syncPointing(bug, force) {
    const key = bug ? `${bug.id}:${bug.status}:${(bug.interactions || []).length}:${$('phone').parentElement.clientHeight}` : '';
    if (key === P.key && !force) return;
    P.key = key;
    const screen = $('phone-screen');
    if (!bug) { screen.innerHTML = ''; $('replay-note').innerHTML = ''; return; }
    const { vw, vh } = viewportOf(bug);
    // as tall as the panel allows, in the phone's real proportions (bezel, controls and padding taken off)
    const room = Math.max(360, Math.min(820, $('phone').parentElement.clientHeight - 118));
    const scale = Math.min(380 / vw, room / vh);
    screen.style.width = `${Math.round(vw * scale)}px`;
    screen.style.height = `${Math.round(vh * scale)}px`;
    const items = bug.interactions || [];
    let n = 0, svg = '';
    const lastScreen = [...items].reverse().find((i) => i.screen)?.screen || bug.screen;
    const lastTap = new Map(); // a tap is drawn only if it is that element's latest tap and it left it selected
    items.forEach((i, k) => { if (i.kind === 'tap' && i.elementId) lastTap.set(i.elementId, k); });
    const placed = []; // label and dot boxes already drawn, so new ones can step out of the way
    const hits = (b) => placed.some((p) => b.x < p.x + p.w && b.x + b.w > p.x && b.y < p.y + p.h && b.y + b.h > p.y);
    const labelAt = (text, x, y) => { // y = baseline; nudges down a line at a time until it is clear
      const w = Math.min(vw - 16, text.length * 14 + 10), h = 32;
      const box = { x: Math.max(8, Math.min(x, vw - w - 8)), y: Math.max(40, Math.min(y, vh - 10)) - 25, w, h };
      for (let tries = 0; tries < 12 && hits(box) && box.y + h + 34 < vh; tries++) box.y += 34;
      placed.push(box);
      return `<text class="pt-label" x="${box.x.toFixed(1)}" y="${(box.y + 25).toFixed(1)}">${esc(text)}</text>`;
    };
    const dotAt = (n, x, y) => {
      const box = { x: x - 20, y: y - 20, w: 40, h: 40 };
      for (let tries = 0; tries < 8 && hits(box) && box.x + 84 < vw; tries++) box.x += 44;
      placed.push(box);
      const cx = box.x + 20, cy = box.y + 20;
      return `<circle class="pt-dot" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="20"/><text class="pt-num" x="${cx.toFixed(1)}" y="${(cy + 8).toFixed(1)}">${n}</text>`;
    };
    for (const [k, i] of items.entries()) {
      if (i.kind === 'tap' && (i.selected === false || (i.elementId && lastTap.get(i.elementId) !== k))) continue;
      const delay = `animation-delay:${(n * 0.45).toFixed(2)}s`;
      if (i.kind === 'tap') {
        const r = rectOf(i, vw, vh);
        if (!r) continue;
        n += 1;
        svg += `<g class="pt" style="${delay}"><rect class="pt-rect" x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}" rx="10"/>
          ${dotAt(n, r.x + Math.min(26, r.w / 2), r.y)}${labelAt(actionTarget(i), r.x + 54, r.y > 60 ? r.y - 26 : r.y + r.h + 34)}</g>`;
      } else if (i.kind === 'circle' && Array.isArray(i.path) && i.path.length > 2) {
        const pts = i.path.filter((p) => Array.isArray(p) && num(p[0]) !== null && num(p[1]) !== null).map((p) => [p[0] * vw, p[1] * vh]);
        if (pts.length < 3) continue;
        n += 1;
        const maxY = Math.max(...pts.map((p) => p[1])), minX = Math.min(...pts.map((p) => p[0]));
        svg += `<g class="pt" style="${delay}"><polygon class="pt-loop" style="${delay}" pathLength="1" points="${pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')}"/>
          ${dotAt(n, pts[0][0], pts[0][1])}${labelAt(actionTarget(i), minX + 6, maxY + 34)}</g>`;
      }
    }
    screen.innerHTML = `<svg viewBox="0 0 ${vw} ${vh}" width="100%" height="100%" role="img" aria-label="Elements the person tapped or circled">
      <text class="pt-screen" x="22" y="52">${esc(titleCase(lastScreen || ''))}</text>${svg}</svg>`;
    $('ctl-replay').hidden = n === 0;
    $('ctl-info').textContent = n ? `${n} pointed at` : '';
    if (n) $('replay-note').innerHTML = '';
    else if (bug.status === 'open') $('replay-note').innerHTML = '<i class="rec-dot"></i><b>Capturing taps and circles</b><span>Tap an element or draw a loop around it on the phone.</span>';
    else $('replay-note').innerHTML = '<b>Nothing was pointed at</b><span>This ticket is speech only.</span>';
  }
  let resizeTimer = 0;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => syncPointing(bugById(S.selectedId)), 150); });
  $('ctl-replay').addEventListener('click', () => syncPointing(bugById(S.selectedId), true));

  // ------------------------------------------------------------ assist feed
  function assistHtml(a) {
    let foot;
    if (a.elementId) {
      const shown = a.shown === true ? '<span class="ok">Highlighted on the phone</span>'
        : a.shown === false ? '<span class="bad">Not found on the phone</span>'
        : (S.clients.app || 0) > 0 ? '<span>Sent to the phone…</span>' : '<span>Phone offline</span>';
      foot = `${elementChip(a.elementId)}${shown}`;
    } else foot = '<span class="chip none">No matching element</span>';
    return `${a.question ? `<div class="q">“${esc(a.question)}”</div>` : ''}
      <div class="a">${esc(a.say || (a.elementId ? 'Here it is.' : ''))}</div>
      <div class="foot">${foot}<span data-ago="${a.at}">${esc(ago(a.at))}</span></div>`;
  }
  function renderAssists(animateId) {
    const host = $('assist-list');
    host.innerHTML = S.assists.length
      ? S.assists.map((a) => `<div class="assist${a.id === animateId ? ' enter' : ''}" data-id="${esc(a.id)}">${assistHtml(a)}</div>`).join('')
      : '<div class="empty">During a session, ask “where is…?” or “how do I…?” and the answer shows up here.</div>';
    $('assist-count').textContent = S.assists.length ? String(S.assists.length) : '';
  }

  // ------------------------------------------------------------ socket
  function onMessage(msg) {
    switch (msg.type) {
      case 'hello': {
        Object.assign(S, {
          bugs: msg.state.bugs || [], openBugId: msg.state.openBugId || null, band: msg.state.band || {},
          session: msg.state.session || null, sessions: msg.state.sessions || [],
          appContext: msg.state.appContext || null, assists: msg.state.assists || [], clients: msg.state.clients || {},
          interim: '', clockOffset: (msg.ts || Date.now()) - Date.now(),
        });
        if (!bugById(S.selectedId) || bugById(S.selectedId).status === 'cancelled') S.selectedId = S.openBugId || S.bugs.find((b) => b.status !== 'cancelled')?.id || null;
        renderAll();
        break;
      }
      case 'bug.upsert': {
        const bug = msg.bug;
        const i = S.bugs.findIndex((b) => b.id === bug.id);
        const before = i >= 0 ? S.bugs[i] : null;
        if (before) S.bugs[i] = bug; else S.bugs.unshift(bug);
        if (bug.status === 'open') S.openBugId = bug.id;
        else if (S.openBugId === bug.id) {
          S.openBugId = null;
          S.interim = '';
          S.flash = { kind: bug.status, reason: bug.cancelReason, at: now() };
        }
        if (!before) S.selectedId = bug.id; // follow the newest ticket
        if (bug.status === 'cancelled' && S.selectedId === bug.id) S.selectedId = S.bugs.find((b) => b.status !== 'cancelled')?.id || null;
        if (before && before.fix?.state !== bug.fix?.state && ['done', 'failed'].includes(bug.fix?.state)) {
          S.fixFlash = { id: bug.id, at: now() }; // shown on the big pill for 8 s
        }
        const justSubmitted = bug.status === 'submitted' && before?.status !== 'submitted';
        renderList(!before || justSubmitted ? bug.id : null);
        renderDetail();
        renderLive();
        break;
      }
      case 'transcript':
        if (msg.final) {
          S.interim = '';
          if (!msg.bugId) S.idleLines.push({ text: msg.text, at: now() });
        } else S.interim = msg.text;
        renderLive();
        break;
      case 'gesture': showGesture(msg); break;
      case 'session.started':
        S.session = { id: msg.sessionId, startedAt: msg.startedAt };
        renderLive(); renderList();
        break;
      case 'session.ended':
        S.session = null;
        S.sessions.unshift({ id: msg.sessionId, startedAt: msg.startedAt, endedAt: msg.endedAt, tickets: msg.tickets });
        S.flash = { kind: 'session-ended', tickets: msg.tickets, at: now() };
        renderLive(); renderList();
        break;
      case 'band.state': S.band = msg; renderHeader(); break;
      case 'app.context': S.appContext = msg; renderHeader(); break;
      case 'clients': S.clients = msg.clients || {}; renderHeader(); renderAssists(); break;
      case 'assist':
      case 'assist.none': {
        const entry = { id: msg.assistId || `local_${msg.ts}`, at: msg.ts || now(), question: msg.question || '', say: msg.say || '', elementId: msg.elementId || null, shown: null };
        S.assists.unshift(entry);
        S.assists.length = Math.min(S.assists.length, 30);
        renderAssists(entry.id);
        break;
      }
      case 'app.assist.shown': {
        const entry = S.assists.find((a) => (msg.assistId ? a.id === msg.assistId : a.elementId === msg.elementId));
        if (entry) { entry.shown = msg.ok; renderAssists(); }
        break;
      }
      default: break; // unknown types are ignored
    }
  }

  function renderAll() { renderHeader(); renderLive(); renderList(); renderDetail(); renderAssists(); }

  let retry = 500;
  function connect() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws?role=dashboard`);
    ws.onopen = () => { S.wsUp = true; retry = 500; renderHeader(); };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg && typeof msg.type === 'string') onMessage(msg);
    };
    ws.onclose = () => {
      S.wsUp = false;
      renderHeader();
      setTimeout(connect, retry);
      retry = Math.min(retry * 1.6, 3000);
    };
    ws.onerror = () => ws.close();
  }

  async function loadKb() {
    try {
      const res = await fetch('/api/kb?optional=1');
      const list = res.ok ? await res.json() : [];
      if (!Array.isArray(list) || !list.length) throw new Error('kb not ready');
      S.kb = Object.fromEntries(list.filter((e) => e && e.id).map((e) => [e.id, e]));
      $('dbg-elements').innerHTML = list.map((e) => `<option value="${esc(e.id)}">${esc(e.label || '')}</option>`).join('');
      const host = $('detail-text');
      host._html = '';
      renderDetail();
      renderAssists();
    } catch (e) { setTimeout(loadKb, 15000); } // the app may not be built yet
  }

  // tick: recording timer, relative times, expiring banners
  setInterval(() => {
    renderLive();
    document.querySelectorAll('[data-ago]').forEach((el) => {
      const text = ago(Number(el.dataset.ago));
      if (el.textContent !== text) el.textContent = text;
    });
    const sel = bugById(S.selectedId);
    if (sel && ((sel.status === 'submitted' && !sel.summary) || ['queued', 'working'].includes(sel.fix?.state))) renderDetail();
  }, 500);

  // ------------------------------------------------------------ debug panel
  const dbg = $('debug');
  function status(text, bad) { $('dbg-status').textContent = text; $('dbg-status').classList.toggle('err', !!bad); }
  async function api(path, body) {
    try {
      const res = await fetch(`/api/debug/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      const data = await res.json();
      const extra = data.error || (data.accepted === false ? 'ignored by the state machine' : 'ok');
      status(`${path} ${JSON.stringify(body || {})} → ${extra}`, !res.ok);
      return data;
    } catch (e) { status(`${path} failed: hub unreachable`, true); return {}; }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  dbg.querySelectorAll('[data-gesture]').forEach((b) => b.addEventListener('click', () => api('gesture', { name: b.dataset.gesture })));
  function sendLine(final) {
    const text = $('dbg-text').value.trim();
    if (!text) return;
    api('transcript', { text, final });
    if (final) $('dbg-text').value = '';
  }
  $('dbg-interim').addEventListener('click', () => sendLine(false));
  $('dbg-final').addEventListener('click', () => sendLine(true));
  $('dbg-text').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendLine(true); });
  $('dbg-assist').addEventListener('click', () => {
    const elementId = $('dbg-element').value.trim();
    const say = $('dbg-say').value.trim() || (elementId ? `Here is ${S.kb[elementId]?.label || elementId}.` : 'I could not find that in the app.');
    api('assist', elementId ? { elementId, say } : { say });
  });
  const dbgTarget = () => $('dbg-element').value.trim() || S.appContext?.lastTappedElementId || S.appContext?.visibleElementIds?.[0] || Object.keys(S.kb)[0] || 'demo-element';
  const sampleTap = (id, y = 0.47) => ({ at: now(), kind: 'tap', screen: S.appContext?.screen || S.kb[id]?.screen || 'home', elementId: id, label: elName(id), rect: { x: 0.08, y, w: 0.84, h: 0.07 } });
  function sampleCircle(id, cy = 0.5) {
    const path = [];
    for (let k = 0; k <= 20; k++) { const a = (k / 20) * Math.PI * 2 - 1.2; path.push([+(0.5 + 0.44 * Math.cos(a)).toFixed(3), +(cy + 0.075 * Math.sin(a)).toFixed(3)]); }
    return { at: now(), kind: 'circle', screen: S.appContext?.screen || S.kb[id]?.screen || 'home', elementIds: [id], path };
  }
  $('dbg-tap').addEventListener('click', () => api('interaction', { items: [sampleTap(dbgTarget())] }));
  $('dbg-circle').addEventListener('click', () => api('interaction', { items: [sampleCircle(dbgTarget())] }));
  $('dbg-fix').addEventListener('click', async () => {
    const bug = bugById(S.selectedId);
    if (!bug) { status('select a ticket first', true); return; }
    await api('fix', { bugId: bug.id, state: 'working' });
    await sleep(2500);
    await api('fix', { bugId: bug.id, state: 'done', summary: 'Simulated fix from the debug panel.', files: ['mobile-app/src/App.tsx'], ms: 2500 });
  });
  $('dbg-band').addEventListener('click', () => {
    const on = !S.band.connected;
    api('band', { connected: on, listening: on, battery: 82, transport: 'debug' });
  });

  function writeUp(bug) {
    const text = spoken(bug) || 'No description was given.';
    const first = text.split(/(?<=[.!?])\s+/)[0].replace(/[.!?]+$/, '');
    const title = first.length > 64 ? `${first.slice(0, 61).trimEnd()}…` : first;
    const elementIds = bug.elementIds?.length ? bug.elementIds
      : [bug.contextAtOpen?.lastTappedElementId || bug.contextAtOpen?.visibleElementIds?.[0]].filter(Boolean);
    const severity = /crash|broken|nothing happens|can't|cannot|stuck|lost|wrong/i.test(text) ? 'high' : 'medium';
    const kind = /should|could|would be|make |add |change|bigger|smaller|colou?r|easier|prefer/i.test(text) && severity !== 'high' ? 'improvement' : 'bug';
    return { bugId: bug.id, kind, title: title.charAt(0).toUpperCase() + title.slice(1), summary: text, severity, elementIds };
  }
  $('dbg-enrich').addEventListener('click', () => {
    const bug = bugById(S.selectedId);
    if (bug) api('enrich', writeUp(bug)); else status('select a ticket first', true);
  });

  async function say(sentence) {
    const words = sentence.split(' ');
    for (let i = 2; i < words.length; i += 2) {
      await api('transcript', { text: words.slice(0, i).join(' '), final: false });
      await sleep(170);
    }
    await api('transcript', { text: sentence, final: true });
    await sleep(450);
  }
  $('dbg-demo').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      if (S.openBugId) await api('gesture', { name: 'cancel' });
      const opened = await api('gesture', { name: 'open' });
      await sleep(900);
      await say('When I tap this control nothing happens on the screen.');
      await api('interaction', { items: [sampleTap(dbgTarget())] });
      await say('I expected it to respond right away and show the new state.');
      await api('interaction', { items: [sampleCircle(dbgTarget(), 0.505)] });
      await sleep(500);
      await api('gesture', { name: 'submit' });
      await sleep(1400);
      const bug = opened.bug && bugById(opened.bug.id);
      if (bug && !bug.title) await api('enrich', writeUp(bug));
    } finally { btn.disabled = false; }
  });

  let resetArmed = 0;
  $('dbg-reset').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    if (Date.now() - resetArmed < 3000) {
      resetArmed = 0;
      btn.textContent = 'Reset all';
      api('reset');
    } else {
      resetArmed = Date.now();
      btn.textContent = 'Click again to erase';
      setTimeout(() => { if (resetArmed) { resetArmed = 0; btn.textContent = 'Reset all'; } }, 3000);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if (e.key === 'Escape' && !dbg.hidden) { dbg.hidden = true; document.activeElement?.blur?.(); return; }
    if (typing) return;
    if (e.key === 'd' || e.key === 'D' || e.key === '`') { dbg.hidden = !dbg.hidden; e.preventDefault(); }
  });
  if (new URLSearchParams(location.search).has('debug')) dbg.hidden = false;

  renderAll();
  connect();
  loadKb();
})();
