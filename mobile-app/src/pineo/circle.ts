// Selector mode. While a ticket is open the page stops behaving like an app:
//   - a tap SELECTS the nearest data-pineo-id element (tap again to deselect) instead of operating it;
//   - a long press (finger resting ~0.35 s) selects too;
//   - hold, then drag a loop: everything inside the loop is selected;
//   - a drag that moves straight away is an ordinary scroll and is left alone;
//   - navigation chrome (elements whose screen is "*" in elements.json: tab bar, header bell) still works
//     and is never selected, so the person can move between screens.
// Input is read from three event families at once — touch, pointer and click — and de-duplicated,
// because no single one is dependable on every phone browser. Every raw event is reported through
// client.diag() so a real device can be watched remotely.
// Nothing here acts unless client.getState().bug is set, so the app is untouched outside a ticket.
import { ELEMENTS, fracRect, type PineoClient } from "./client";

const HOLD_MS = 220; // how long the finger must rest before a drag becomes a stroke
const LONG_PRESS_MS = 350; // resting this long without moving selects what is under the finger
const SLOP = 12; // px a finger may wander and still count as a tap / a resting finger
const TAP_MS = 700; // longer than this without a long-press is not a tap
const STROKE = "#087f4a"; // strong green that stays visible on light surfaces
const STROKE_GLOW = "rgba(8, 127, 74, 0.55)";
const NAV_IDS = new Set(ELEMENTS.filter((e) => e.screen === "*").map((e) => e.id));

type Pt = [number, number];

/** Event targets can be text nodes on some browsers; always work with an Element. */
const asEl = (t: EventTarget | null): Element | null =>
  t instanceof Element ? t : t && (t as Node).nodeType === 3 ? (t as Node).parentElement : null;
const idOf = (t: EventTarget | null) => asEl(t)?.closest("[data-pineo-id]")?.getAttribute("data-pineo-id") ?? null;
const inOverlay = (t: EventTarget | null) => !!asEl(t)?.closest(".pineo-root");
const isNav = (t: EventTarget | null) => NAV_IDS.has(idOf(t) ?? "");
const describe = (el: Element | null | undefined) =>
  el ? `${el.tagName.toLowerCase()}${typeof el.className === "string" && el.className ? "." + el.className.split(/\s+/)[0] : ""}` : null;

const ownLayer = (e: Element) => !!e.closest(".pineo-root") || e.classList.contains("pineo-canvas");

/** Topmost app element at a viewport point (our own overlay nodes skipped). */
function topAt(x: number, y: number): Element | null {
  const list = document.elementsFromPoint ? document.elementsFromPoint(x, y) : [document.elementFromPoint(x, y)].filter(Boolean) as Element[];
  return list.find((e) => !ownLayer(e)) ?? null;
}

/** The marked element at a point: the smallest one containing it, else the nearest within reach. */
export function elementAt(x: number, y: number): HTMLElement | null {
  const top = topAt(x, y);
  // Buttons and fields are transparent to hit-testing in selector mode, so `top` is whatever lies behind
  // them; anything inside the same marked block as `top` is fair game. With no `top` at all (should not
  // happen) fall back to pure geometry.
  const scope = top ? (top.closest("[data-pineo-id]") ?? top) : document.body;
  let best: HTMLElement | null = null;
  let bestScore = Infinity;
  for (const el of document.querySelectorAll<HTMLElement>("[data-pineo-id]")) {
    if (top && !(scope.contains(el) || el.contains(top))) continue; // skip things hidden behind something else
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const dx = Math.max(r.left - x, 0, x - r.right);
    const dy = Math.max(r.top - y, 0, y - r.bottom);
    const dist = Math.hypot(dx, dy);
    if (dist > 28) continue;
    const score = dist * 1e7 + r.width * r.height; // containing beats near; smaller beats larger
    if (score < bestScore) {
      bestScore = score;
      best = el;
    }
  }
  return best;
}

export function watchCircles(client: PineoClient) {
  const canvas = document.createElement("canvas");
  canvas.className = "pineo-canvas";
  document.body.appendChild(canvas); // its own layer: React owns everything inside .pineo-root
  const ctx = canvas.getContext("2d")!;
  const open = () => !!client.getState().bug;

  // one gesture at a time (the most recent finger; a palm resting on the edge must not block it)
  let g: { id: number; start: Pt; last: Pt; at: number; armed: boolean; selected: boolean; pressId: string | null; scrolled: boolean } | null = null;
  let pts: Pt[] = [];
  let holdTimer: number | undefined;
  let pressTimer: number | undefined;
  let fadeTimer: number | undefined;
  let lastTap = { at: 0, x: 0, y: 0 }; // for de-duplicating touch / pointer / click reports of one tap
  let lastGestureEnd = 0;

  const diag = (ev: string, x: number, y: number, hit: string | null = null, extra?: string) => {
    if (!open() && !client.getState().debugOpen) return;
    client.diag({ ev, x: Math.round(x), y: Math.round(y), hit, top: describe(topAt(x, y)), extra });
  };
  /** Never let one bad event break selecting; report it instead. */
  const safe = <E extends Event>(name: string, fn: (e: E) => void) => (e: E) => {
    try {
      fn(e);
    } catch (err) {
      client.diag({ ev: "error", x: 0, y: 0, hit: null, top: null, extra: `${name}: ${String(err).slice(0, 120)}` });
    }
  };

  const size = () => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  size();
  window.addEventListener("resize", size);

  const draw = () => {
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    if (!pts.length) return;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.shadowColor = STROKE_GLOW;
    ctx.shadowBlur = 12;
    ctx.strokeStyle = STROKE;
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (const p of pts) ctx.lineTo(p[0], p[1]);
    if (pts.length === 1) ctx.arc(pts[0][0], pts[0][1], 14, 0, Math.PI * 2); // "armed" ring under the finger
    ctx.stroke();
  };
  const clearStroke = () => {
    pts = [];
    draw();
  };
  const endGesture = () => {
    clearTimeout(holdTimer);
    clearTimeout(pressTimer);
    g = null;
    lastGestureEnd = Date.now();
  };

  /** Select (or deselect) whatever is at the point. Returns the id if this call selected it. */
  const selectAt = (x: number, y: number): string | null => {
    const el = elementAt(x, y);
    const id = el?.getAttribute("data-pineo-id");
    if (!el || !id || NAV_IDS.has(id)) {
      diag("nohit", x, y, id ?? null);
      return null;
    }
    client.toggleSelect(id, fracRect(el.getBoundingClientRect()));
    const on = client.getState().selected.includes(id);
    diag(on ? "select" : "deselect", x, y, id);
    return on ? id : null;
  };

  /** One tap, however many event families report it. */
  const tap = (x: number, y: number, source: string) => {
    const now = Date.now();
    if (now - lastTap.at < 500 && Math.hypot(x - lastTap.x, y - lastTap.y) < 30) return; // already handled
    lastTap = { at: now, x, y };
    diag("tap", x, y, null, source);
    selectAt(x, y);
  };

  const inside = (x: number, y: number, poly: Pt[]) => {
    let hit = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i];
      const [xj, yj] = poly[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  };

  /** Finish a stroke. Returns false when it was too short to be a loop (the caller treats it as a tap). */
  const finish = (): boolean => {
    const path = pts;
    let len = 0;
    for (let i = 1; i < path.length; i++) len += Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]);
    if (path.length < 6 || len < 70) {
      clearStroke();
      return false;
    }
    const xs = path.map((p) => p[0]);
    const ys = path.map((p) => p[1]);
    const box = new DOMRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    const boxArea = Math.max(1, box.width * box.height);

    // elements whose centre is inside the loop, ignoring ones far bigger than the loop itself
    let picked: HTMLElement[] = [];
    for (const el of document.querySelectorAll<HTMLElement>("[data-pineo-id]")) {
      if (NAV_IDS.has(el.getAttribute("data-pineo-id")!)) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || r.bottom < 0 || r.top > window.innerHeight) continue;
      if (r.width * r.height > boxArea * 4) continue;
      if (inside(r.left + r.width / 2, r.top + r.height / 2, path)) picked.push(el);
    }
    // prefer the specific things over the group that contains them
    picked = picked.filter((el) => !picked.some((other) => other !== el && el.contains(other)));
    if (!picked.length) {
      const under = elementAt(box.left + box.width / 2, box.top + box.height / 2); // an open or tiny loop
      if (under && !NAV_IDS.has(under.getAttribute("data-pineo-id")!)) picked = [under];
    }
    const ids = [...new Set(picked.map((el) => el.getAttribute("data-pineo-id")!))];

    const step = Math.max(1, Math.ceil(path.length / 40));
    const simple = path
      .filter((_, i) => i % step === 0)
      .map(([x, y]) => [Math.round((x / window.innerWidth) * 1000) / 1000, Math.round((y / window.innerHeight) * 1000) / 1000] as Pt);
    client.selectMany(ids, fracRect(box), simple);
    diag("circle", box.left + box.width / 2, box.top + box.height / 2, ids.join(",") || null);

    // close the loop visually, then let it fade
    pts = [...path, path[0]];
    draw();
    fadeTimer = window.setTimeout(() => {
      canvas.style.transition = "opacity 0.7s";
      canvas.style.opacity = "0";
      fadeTimer = window.setTimeout(clearStroke, 750);
    }, 500);
    return true;
  };

  // ---- shared gesture logic (fed by touch events, or by mouse events on desktop) ----
  const down = (id: number, x: number, y: number, hold: boolean) => {
    clearTimeout(holdTimer);
    clearTimeout(pressTimer);
    g = { id, start: [x, y], last: [x, y], at: Date.now(), armed: false, selected: false, pressId: null, scrolled: false };
    if (!hold) return;
    const mine = g;
    holdTimer = window.setTimeout(() => {
      if (g !== mine || mine.scrolled) return;
      clearTimeout(fadeTimer);
      canvas.style.transition = "none";
      canvas.style.opacity = "1";
      mine.armed = true;
      pts = [mine.start];
      draw();
    }, HOLD_MS);
    // long press: still resting → select now (pulse, and a buzz where the phone supports it);
    // dragging on from here still draws a circle
    pressTimer = window.setTimeout(() => {
      if (g !== mine || mine.scrolled || pts.length > 1) return;
      mine.selected = true;
      lastTap = { at: Date.now(), x: mine.start[0], y: mine.start[1] };
      diag("longpress", mine.start[0], mine.start[1]);
      mine.pressId = selectAt(mine.start[0], mine.start[1]);
      navigator.vibrate?.(15);
    }, LONG_PRESS_MS);
  };

  /** Returns true when the move belongs to a stroke (the caller must then prevent scrolling). */
  const move = (x: number, y: number): boolean => {
    if (!g || g.scrolled) return false;
    g.last = [x, y];
    const far = Math.hypot(x - g.start[0], y - g.start[1]);
    if (!g.armed) {
      if (far > SLOP) {
        g.scrolled = true; // moving before the hold completed: an ordinary scroll
        clearTimeout(holdTimer);
        clearTimeout(pressTimer);
        return false;
      }
      return false; // not ours yet: until the hold completes the browser keeps full control (a slow drag must still scroll)
    }
    if (far < 4 && pts.length <= 1) return true; // still resting
    pts.push([x, y]);
    draw();
    return true;
  };

  const up = (x: number, y: number, cancelled: boolean, source: string) => {
    const cur = g;
    if (!cur) return;
    const pressed = cur.pressId;
    const wasLoop = cur.armed && finish();
    endGesture();
    if (cur.scrolled || cancelled) return;
    // The hold turned into a circle: the loop says what is meant, not the spot where the finger rested.
    if (wasLoop) {
      if (pressed && !client.lastCircle.includes(pressed)) client.toggleSelect(pressed, undefined);
      return;
    }
    if (cur.selected) return; // the long press already selected it
    const moved = Math.hypot(x - cur.start[0], y - cur.start[1]);
    if (moved <= SLOP && Date.now() - cur.at < TAP_MS) tap(cur.start[0], cur.start[1], source);
  };

  // ---- 1. touch events ----
  const tracked = (list: TouchList) => {
    for (let i = 0; i < list.length; i++) if (g && list[i].identifier === g.id) return list[i];
    return null;
  };
  document.addEventListener(
    "touchstart",
    safe("touchstart", (e: TouchEvent) => {
      const t = e.changedTouches[0];
      if (!t) return;
      diag("touchstart", t.clientX, t.clientY, idOf(e.target), `n=${e.touches.length}`);
      if (!open() || inOverlay(e.target) || isNav(e.target)) {
        g = null;
        return;
      }
      down(t.identifier, t.clientX, t.clientY, true);
    }),
    { capture: true, passive: false },
  );
  document.addEventListener(
    "touchmove",
    safe("touchmove", (e: TouchEvent) => {
      const t = tracked(e.changedTouches);
      if (!t || !open()) return;
      if (move(t.clientX, t.clientY) && e.cancelable) e.preventDefault(); // ours: no scrolling
    }),
    { capture: true, passive: false },
  );
  const touchEnd = safe("touchend", (e: TouchEvent) => {
    const t = tracked(e.changedTouches);
    if (!t || !g) return;
    diag(e.type, t.clientX, t.clientY, null, g.scrolled ? "scrolled" : g.armed ? "armed" : "");
    const scrolled = g.scrolled;
    // The app must not see this touch (no click, no focus, no keyboard) — unless it was a scroll.
    if (!scrolled && e.cancelable) e.preventDefault();
    up(t.clientX, t.clientY, e.type === "touchcancel", "touch");
  });
  document.addEventListener("touchend", touchEnd, { capture: true, passive: false });
  document.addEventListener("touchcancel", touchEnd, { capture: true, passive: false });

  // ---- 2. pointer events (a second opinion: some browsers deliver these when touch events go missing) ----
  let pd: { id: number; x: number; y: number; at: number; nav: boolean } | null = null;
  document.addEventListener(
    "pointerdown",
    safe("pointerdown", (e: PointerEvent) => {
      if (e.pointerType === "mouse") return;
      diag("pointerdown", e.clientX, e.clientY, idOf(e.target));
      pd = open() && !inOverlay(e.target) ? { id: e.pointerId, x: e.clientX, y: e.clientY, at: Date.now(), nav: isNav(e.target) } : null;
    }),
    true,
  );
  document.addEventListener(
    "pointerup",
    safe("pointerup", (e: PointerEvent) => {
      if (e.pointerType === "mouse") return;
      diag("pointerup", e.clientX, e.clientY, idOf(e.target));
      const p = pd;
      pd = null;
      if (!p || p.id !== e.pointerId || p.nav || !open()) return;
      // Only act when the touch path is not already tracking this finger.
      if (g || Date.now() - lastGestureEnd < 60) return;
      if (Math.hypot(e.clientX - p.x, e.clientY - p.y) <= SLOP && Date.now() - p.at < TAP_MS) tap(p.x, p.y, "pointer");
    }),
    true,
  );
  document.addEventListener(
    "pointercancel",
    safe("pointercancel", (e: PointerEvent) => {
      diag("pointercancel", e.clientX, e.clientY); // normal when the browser starts scrolling
      pd = null;
    }),
    true,
  );

  // ---- mouse (desktop): click selects, drag draws, hover shows a dashed outline ----
  let mouseDown = false;
  const afterTouch = () => Date.now() - lastGestureEnd < 700 || Date.now() - lastTap.at < 700;
  document.addEventListener(
    "mousedown",
    safe("mousedown", (e: MouseEvent) => {
      if (!open() || inOverlay(e.target) || isNav(e.target)) return;
      e.preventDefault(); // no focus, no text selection
      e.stopImmediatePropagation();
      if (e.button !== 0 || afterTouch()) return;
      mouseDown = true;
      down(-1, e.clientX, e.clientY, false);
    }),
    true,
  );
  document.addEventListener(
    "mousemove",
    safe("mousemove", (e: MouseEvent) => {
      if (!open()) return;
      if (!mouseDown || !g) {
        if (!afterTouch()) client.setHover(inOverlay(e.target) ? null : (elementAt(e.clientX, e.clientY)?.getAttribute("data-pineo-id") ?? null));
        return;
      }
      if (!g.armed) {
        if (Math.hypot(e.clientX - g.start[0], e.clientY - g.start[1]) < SLOP) return;
        clearTimeout(fadeTimer);
        canvas.style.transition = "none";
        canvas.style.opacity = "1";
        g.armed = true;
        pts = [g.start];
        client.setHover(null);
      }
      e.preventDefault();
      pts.push([e.clientX, e.clientY]);
      draw();
    }),
    true,
  );
  document.addEventListener(
    "mouseup",
    safe("mouseup", (e: MouseEvent) => {
      if (!mouseDown) return;
      mouseDown = false;
      if (open()) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
      up(e.clientX, e.clientY, false, "mouse");
    }),
    true,
  );

  // ---- 3. click: swallowed so the app never reacts, and a last-resort way to select ----
  document.addEventListener(
    "click",
    safe("click", (e: MouseEvent) => {
      if (!open() || inOverlay(e.target) || isNav(e.target)) return;
      diag("click", e.clientX, e.clientY, idOf(e.target));
      e.preventDefault();
      e.stopImmediatePropagation();
      if (e.detail !== 0 || e.clientX || e.clientY) tap(e.clientX, e.clientY, "click"); // de-duplicated against touch/pointer
    }),
    true,
  );
  const swallow = (e: Event) => {
    if (!open() || inOverlay(e.target) || isNav(e.target)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
  };
  for (const type of ["dblclick", "auxclick", "contextmenu", "submit", "dragstart", "selectstart"]) {
    document.addEventListener(type, swallow, true);
  }
  document.addEventListener(
    "focusin",
    (e) => {
      if (open() && !inOverlay(e.target)) (asEl(e.target) as HTMLElement | null)?.blur?.(); // never bring up the keyboard
    },
    true,
  );

  // entering the mode: tag navigation chrome so pineo.css keeps it tappable
  const tagNav = () => {
    for (const el of document.querySelectorAll("[data-pineo-id]")) {
      if (NAV_IDS.has(el.getAttribute("data-pineo-id")!)) el.setAttribute("data-pineo-nav", "");
    }
  };
  let wasOpen = false;
  client.subscribe(() => {
    if (open() && !wasOpen) tagNav();
    if (!open() && wasOpen) {
      // leaving the mode: drop any stroke in progress
      endGesture();
      clearTimeout(fadeTimer);
      clearStroke();
    }
    wasOpen = open();
  });
}
