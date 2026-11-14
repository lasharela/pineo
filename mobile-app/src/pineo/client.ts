// Pineo client: talks to the hub over WebSocket, reports what is on screen,
// reports taps, screen changes and drawn circles while a ticket is open, and points at elements on request.
// Nothing in here knows about the host app beyond the small PineoHost adapter.
import elementsJson from "./elements.json";

export interface PineoHost {
  /** Current screen name, matching `screen` in elements.json. */
  getScreen(): string;
  /** Switch to another screen. */
  navigate(screen: string): void;
  /** Call `cb` after every screen change; returns an unsubscribe function. */
  onScreenChange(cb: () => void): () => void;
}

export interface PineoElement {
  id: string;
  screen: string;
  label: string;
  description: string;
  howTo: string;
  keywords: string[];
}

export const ELEMENTS = elementsJson as PineoElement[];
/** When this copy of the Pineo code was loaded by the page (a full reload or a hot update of this file renews it). */
export const LOADED_AT = new Date();
export const LOADED_LABEL = LOADED_AT.toLocaleTimeString([], { hour12: false });

const ELEMENT_BY_ID = new Map(ELEMENTS.map((e) => [e.id, e]));

export interface PineoState {
  connected: boolean;
  band: { connected: boolean; listening: boolean; battery?: number };
  bug: { bugId: string; startedAt: number } | null;
  finals: string[];
  interim: string;
  captionAt: number;
  flash: { kind: "submitted" | "cancelled"; key: number } | null;
  /** A debugging session is running (the app stays fully usable; tickets are opened inside it). */
  session: { id: string; startedAt: number } | null;
  sessionEnded: { tickets: number; key: number } | null;
  /** The closing gesture happened; waiting for the hub to say submitted or cancelled. */
  closing: boolean;
  /** Progress of the coding agent on the last submitted ticket. */
  fix: { state: "queued" | "working" | "done" | "failed"; summary: string; key: number; slow?: boolean } | null;
  /** Elements picked by the last drawn circle (shown briefly), or flashed after a fix. */
  picked: { ids: string[]; key: number } | null;
  /** Selector mode: elements the person has tapped or circled in the open ticket. */
  selected: string[];
  /** Desktop only: element under the mouse while selecting. */
  hover: string | null;
  assist: { key: number; elementId: string | null; say: string } | null;
  debugOpen: boolean;
  log: string[];
}

type Msg = { type: string; [k: string]: unknown };

export interface Interaction {
  at: number;
  kind: "tap" | "circle" | "screen" | "scroll" | "input";
  screen: string;
  elementId?: string;
  label?: string;
  elementIds?: string[];
  /** Viewport fractions 0..1. */
  rect?: { x: number; y: number; w: number; h: number };
  /** Simplified stroke in viewport fractions 0..1. */
  path?: [number, number][];
  /** On a tap: false when the tap deselected the element. */
  selected?: boolean;
}

const r3 = (n: number) => Math.round(n * 1000) / 1000;
export const fracRect = (r: DOMRect) => ({
  x: r3(r.left / window.innerWidth),
  y: r3(r.top / window.innerHeight),
  w: r3(r.width / window.innerWidth),
  h: r3(r.height / window.innerHeight),
});

const sel = (id: string) => `[data-pineo-id="${CSS.escape(id)}"]`;
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class PineoClient {
  state: PineoState = {
    connected: false,
    band: { connected: false, listening: false },
    bug: null,
    finals: [],
    interim: "",
    captionAt: 0,
    flash: null,
    session: null,
    sessionEnded: null,
    closing: false,
    fix: null,
    picked: null,
    selected: [],
    hover: null,
    assist: null,
    debugOpen: false,
    log: [],
  };

  private listeners = new Set<() => void>();
  private ws: WebSocket | null = null;
  private retry = 0;
  private host: PineoHost;

  // interactions captured while a ticket is open
  private items: Interaction[] = [];
  private pending: { bugId: string; items: Interaction[] }[] = [];
  private flushTimer: number | null = null;
  private touched = new Map<string, string[]>(); // bugId -> element ids tapped or circled
  // remote diagnostics: what the selector saw, so a real phone can be watched from the hub
  private diagItems: object[] = [];
  private diagTimer: number | null = null;
  private diagUaSent = false;
  lastDiag = "";

  /** Queue one raw-event record; sent as `app.diag { items }` at most every 500 ms. */
  diag(item: { ev: string; x: number; y: number; hit: string | null; top: string | null; extra?: string }) {
    const vv = window.visualViewport;
    const rec: Record<string, unknown> = { t: Date.now(), ...item, vv: vv ? [Math.round(vv.offsetLeft), Math.round(vv.offsetTop), Math.round(vv.scale * 100) / 100] : null };
    if (!this.diagUaSent) {
      rec.ua = navigator.userAgent;
      rec.view = [window.innerWidth, window.innerHeight, window.devicePixelRatio];
      this.diagUaSent = true;
    }
    this.diagItems.push(rec);
    if (this.diagItems.length > 200) this.diagItems.splice(0, this.diagItems.length - 200);
    this.lastDiag = `${item.ev} ${item.x},${item.y} hit=${item.hit ?? "-"} top=${item.top ?? "-"}${item.extra ? " " + item.extra : ""}`;
    if (this.state.debugOpen) this.listeners.forEach((l) => l());
    if (!this.diagTimer) {
      this.diagTimer = window.setTimeout(() => {
        this.diagTimer = null;
        if (this.diagItems.length && this.send("app.diag", { bugId: this.state.bug?.bugId ?? null, items: this.diagItems })) this.diagItems = [];
      }, 500);
    }
  }

  /** Element ids picked by the most recent circle. */
  lastCircle: string[] = [];
  private fixTimer: number | null = null;
  private pickTimer: number | null = null;

  // context
  private io: IntersectionObserver | null = null;
  private observed = new Set<Element>();
  private visible = new Set<Element>();
  private lastTapped: string | undefined;
  private ctxTimer: number | null = null;
  private ctxFirstAt = 0;
  private lastCtx = "";
  private rescanQueued = false;

  private assistSeq = 0;
  private assistTimer: number | null = null;
  private flashTimer: number | null = null;

  constructor(host: PineoHost) {
    this.host = host;
  }

  // ---- store -------------------------------------------------------------
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getState = () => this.state;
  private set(patch: Partial<PineoState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((l) => l());
  }
  private log(line: string) {
    const t = new Date().toLocaleTimeString([], { hour12: false });
    this.set({ log: [`${t} ${line}`, ...this.state.log].slice(0, 30) });
  }

  // ---- lifecycle ---------------------------------------------------------
  start() {
    try {
      this.touched = new Map(JSON.parse(sessionStorage.getItem("pineo.touched") ?? "[]"));
      const fix = JSON.parse(sessionStorage.getItem("pineo.fix") ?? "null");
      // only a finished result is worth restoring; "working" is re-read from the hub's hello instead
      if (fix && (fix.state === "done" || fix.state === "failed") && Date.now() - fix.key < 8000) this.onFix({ type: "bug.fix", ...fix });
    } catch {
      /* ignore */
    }
    this.connect();
    this.watchLiveness();
    this.watchContext();
    if (new URLSearchParams(location.search).has("debug")) this.set({ debugOpen: true });
    this.watchTripleTap();
  }

  setDebugOpen(open: boolean) {
    this.set({ debugOpen: open });
  }

  // ---- websocket ---------------------------------------------------------
  private connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws?role=app`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.set({ connected: true });
      this.log("hub connected");
      // tell the hub which code this phone is running, whatever state it is in
      this.send("app.diag", {
        bugId: this.state.bug?.bugId ?? null,
        items: [{ t: Date.now(), ev: "hello", ua: navigator.userAgent, view: [window.innerWidth, window.innerHeight, window.devicePixelRatio], build: LOADED_AT.toISOString(), url: location.origin }],
      });
      this.flush();
      this.lastCtx = "";
      this.scheduleContext(50);
    };
    ws.onmessage = (e) => {
      let msg: Msg;
      try {
        msg = JSON.parse(String(e.data));
      } catch {
        return;
      }
      if (msg && typeof msg.type === "string") this.handle(msg);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.state.connected) this.log("hub disconnected");
      this.set({ connected: false });
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  /** Drop the socket and connect again at once; the hub's hello resyncs everything. */
  private reconnectNow(why: string) {
    const old = this.ws;
    this.ws = null;
    if (old) {
      old.onclose = null;
      old.onmessage = null;
      try {
        old.close();
      } catch {
        /* ignore */
      }
    } else if (this.reconnectTimer === null) return; // a connect attempt is already in flight
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.log(`reconnect: ${why}`);
    this.set({ connected: false });
    this.retry = 0;
    this.connect();
  }

  /**
   * A phone that slept can keep a socket that looks open but is dead. Reconnect when the page comes
   * back, and every 15 s compare our state with the hub's over plain HTTP: any difference means a
   * message was missed, so reconnect and let hello put it right.
   */
  private watchLiveness() {
    let hiddenAt = 0;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt > 3000) this.reconnectNow("page visible again");
    });
    window.addEventListener("pageshow", (e) => {
      if ((e as PageTransitionEvent).persisted) this.reconnectNow("page restored");
    });
    window.addEventListener("online", () => this.reconnectNow("network back"));
    window.setInterval(async () => {
      if (document.visibilityState !== "visible" || !this.state.connected) return;
      try {
        const res = await fetch("/api/state", { cache: "no-store" });
        if (!res.ok) return;
        const hub = (await res.json()) as { openBugId?: string | null; session?: { id?: string } | null; bugs?: { status?: string; fix?: { state?: string } }[] };
        const hubFix = (hub.bugs ?? []).find((b) => b.status === "submitted")?.fix?.state;
        const waiting = this.state.fix?.state === "queued" || this.state.fix?.state === "working";
        const differs =
          (hub.openBugId ?? null) !== (this.state.bug?.bugId ?? null) && !this.state.closing ||
          (hub.session?.id ?? null) !== (this.state.session?.id ?? null) ||
          (waiting && hubFix !== "queued" && hubFix !== "working");
        if (!differs) return (this.drift = 0);
        if (++this.drift >= 2) {
          this.drift = 0;
          this.reconnectNow("state differs from hub"); // twice in a row: not just a message in flight
        }
      } catch {
        /* hub unreachable: the socket's own close handler deals with it */
      }
    }, 15000 / 2);
  }
  private drift = 0;
  private reconnectTimer: number | null = null;

  private scheduleReconnect() {
    const delay = Math.min(5000, 500 * 2 ** this.retry++);
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private send(type: string, fields: object): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ type, ts: Date.now(), ...fields }));
    return true;
  }

  /** Handle one hub message. Public so the debug panel can simulate the hub locally. */
  handle(msg: Msg) {
    switch (msg.type) {
      case "hello":
        this.onHello((msg.state ?? {}) as Record<string, unknown>);
        break;
      case "bug.opened": {
        const id = (msg.bugId ?? (msg.bug as { id?: string } | undefined)?.id) as string | undefined;
        if (id) this.openBug(id, Date.now());
        break;
      }
      case "session.started":
        this.set({ session: { id: String(msg.sessionId ?? ""), startedAt: Number(msg.startedAt) || Date.now() }, sessionEnded: null });
        this.log("session started");
        break;
      case "session.ended": {
        const key = Date.now();
        this.set({ session: null, sessionEnded: { tickets: Number(msg.tickets) || 0, key } });
        this.log("session ended");
        window.setTimeout(() => this.state.sessionEnded?.key === key && this.set({ sessionEnded: null }), 3500);
        break;
      }
      case "bug.closing":
        this.leaveTicket(true);
        break;
      case "bug.submitted":
        // `amended`: the end of a sentence arrived after the ticket closed. Either we already said
        // "Ticket sent" (say nothing again), or we said "Closed — nothing captured" and it is now revived.
        if (msg.amended && !this.state.bug && !this.state.closing) this.amend(String(msg.bugId ?? ""));
        else this.closeBug("submitted", false, String(msg.bugId ?? ""));
        break;
      case "bug.cancelled":
        this.closeBug("cancelled", false, String(msg.bugId ?? ""));
        break;
      case "transcript": {
        const text = String(msg.text ?? "").trim();
        if (!text) break;
        if (msg.final) this.set({ finals: [...this.state.finals, text].slice(-3), interim: "", captionAt: Date.now() });
        else this.set({ interim: text, captionAt: Date.now() });
        break;
      }
      case "assist":
        void this.runAssist(String(msg.elementId ?? ""), String(msg.say ?? ""));
        break;
      case "assist.none":
        this.showAssist(null, String(msg.say ?? "I couldn't find that in this app."));
        break;
      case "bug.fix":
        this.onFix(msg);
        break;
      case "band.state":
        this.set({
          band: {
            connected: !!msg.connected,
            listening: !!msg.listening,
            battery: typeof msg.battery === "number" ? msg.battery : undefined,
          },
        });
        break;
    }
  }

  private onHello(state: Record<string, unknown>) {
    const sess = state.session as { id?: string; startedAt?: number } | null | undefined;
    this.set({ session: sess?.id ? { id: sess.id, startedAt: Number(sess.startedAt) || Date.now() } : null });
    // resync the coding agent's progress: a `done` sent while this page was reloading must not be lost
    type F = { id?: string; status?: string; fix?: { state?: string; summary?: string } };
    const newest = ((Array.isArray(state.bugs) ? state.bugs : []) as F[]).find((b) => b.status === "submitted"); // newest sent ticket
    const shown = this.state.fix?.state;
    const hubFix = newest?.fix?.state;
    if (hubFix === "queued" || hubFix === "working") {
      if (shown !== hubFix) this.onFix({ type: "bug.fix", bugId: newest!.id, state: hubFix });
    } else if (shown === "queued" || shown === "working") {
      // we were waiting; the hub says it finished (or knows nothing about it)
      if (hubFix === "done" || hubFix === "failed") this.onFix({ type: "bug.fix", bugId: newest!.id, state: hubFix, summary: newest!.fix!.summary });
      else this.set({ fix: null });
    }
    const band = (state.band ?? state.bandState) as Record<string, unknown> | undefined;
    if (band) this.set({ band: { connected: !!band.connected, listening: !!band.listening } });
    // The hub may describe the open bug in a few shapes; accept any of them.
    type B = { id?: string; status?: string; openedAt?: number };
    const bugs = (Array.isArray(state.bugs) ? state.bugs : []) as B[];
    const open =
      (state.openBug as B | undefined) ??
      (state.currentBug as B | undefined) ??
      bugs.find((b) => b.status === "open") ??
      (typeof state.openBugId === "string" ? { id: state.openBugId } : undefined);
    if (open?.id && (!open.status || open.status === "open")) {
      if (this.state.bug?.bugId !== open.id) this.openBug(open.id, open.openedAt ?? Date.now());
    } else if (this.state.bug) {
      this.closeBug("cancelled", true);
    }
  }

  // ---- bug report + recording -------------------------------------------
  private openBug(bugId: string, startedAt: number) {
    if (this.state.bug?.bugId === bugId) return;
    if (this.state.bug) this.closeBug("cancelled", true);
    this.set({ bug: { bugId, startedAt }, finals: [], interim: "", flash: null, closing: false, fix: null, selected: [], hover: null });
    (document.activeElement as HTMLElement | null)?.blur?.(); // selector mode: put the keyboard away
    this.log(`ticket opened ${bugId}`);
    this.items = [];
    document.documentElement.classList.add("pineo-ticket");
    document.documentElement.style.setProperty("--pineo-dock-space", "260px"); // host pads its scroller by this // see pineo.css: no text selection / long-press menu while pointing
    this.note({ kind: "screen" });
    this.flushTimer = window.setInterval(() => this.flush(), 700);
  }

  /** Leave selector mode right away and send whatever is still pending. */
  private leaveTicket(closing: boolean): boolean {
    const bug = this.state.bug;
    if (!bug) return false;
    if (this.flushTimer) clearInterval(this.flushTimer);
    this.flushTimer = null;
    this.flush(); // last items, before the ticket id is gone
    document.documentElement.classList.remove("pineo-ticket");
    document.documentElement.style.removeProperty("--pineo-dock-space");
    this.closingId = bug.bugId;
    this.set({ bug: null, closing, selected: [], hover: null });
    if (closing) {
      // The hub may wait up to ~4.5 s for the person to finish the sentence. Safety net only:
      // never sit on "Closing…" for ever if the verdict is lost.
      if (this.closingTimer) clearTimeout(this.closingTimer);
      this.closingTimer = window.setTimeout(() => this.state.closing && this.set({ closing: false }), 6000);
    }
    return true;
  }

  /** A closed ticket got more words: revive a cancelled one as sent; never confirm twice. */
  private amend(bugId: string) {
    if (this.lastVerdict?.bugId === bugId && this.lastVerdict.kind === "submitted") return; // already confirmed
    this.lastVerdict = { bugId, kind: "submitted" };
    this.log(`ticket revived ${bugId}`);
    const key = Date.now();
    this.set({ flash: { kind: "submitted", key } });
    if (this.flashTimer) clearTimeout(this.flashTimer);
    this.flashTimer = window.setTimeout(() => {
      if (this.state.flash?.key === key) this.set({ flash: null });
    }, 2200);
  }

  private closingId = "";
  private closingTimer: number | null = null;
  private lastVerdict: { bugId: string; kind: "submitted" | "cancelled" } | null = null;

  private closeBug(kind: "submitted" | "cancelled", silent = false, bugId = "") {
    const openId = this.state.bug?.bugId ?? this.closingId;
    const wasOpen = this.leaveTicket(false);
    if (!wasOpen && !this.state.closing) return;
    this.lastVerdict = { bugId: bugId || openId || "", kind };
    this.log(`ticket ${kind}`);
    const key = Date.now();
    this.set({ closing: false, flash: silent ? null : { kind, key } });
    if (this.flashTimer) clearTimeout(this.flashTimer);
    this.flashTimer = window.setTimeout(() => {
      if (this.state.flash?.key === key) this.set({ flash: null });
    }, 2200);
  }

  /** Record one thing the person did; only while a ticket is open. */
  note(item: Omit<Interaction, "at" | "screen">) {
    const bug = this.state.bug;
    if (!bug) return;
    const label = item.elementId ? ELEMENT_BY_ID.get(item.elementId)?.label : undefined;
    this.items.push({ at: Date.now(), screen: this.host.getScreen(), label, ...item });
    const ids = [...(item.elementIds ?? []), ...(item.elementId ? [item.elementId] : [])];
    if (ids.length && item.kind !== "screen") {
      let all = this.touched.get(bug.bugId) ?? [];
      if (item.selected === false) all = all.filter((id) => id !== item.elementId);
      else for (const id of ids) if (!all.includes(id)) all.push(id);
      this.touched.set(bug.bugId, all);
      try {
        sessionStorage.setItem("pineo.touched", JSON.stringify([...this.touched].slice(-5)));
      } catch {
        /* ignore */
      }
    }
    if (item.kind === "circle") this.flush();
  }

  private flush() {
    const bugId = this.state.bug?.bugId;
    if (bugId && this.items.length) {
      this.pending.push({ bugId, items: this.items });
      this.items = [];
    }
    while (this.pending.length) {
      if (!this.send("app.interaction", this.pending[0])) break; // keep it for after reconnect
      this.pending.shift();
    }
  }

  // ---- selector mode (driven by circle.ts) --------------------------------
  /** A tap in selector mode: select the element, or deselect it if it already was. */
  toggleSelect(id: string, rect: Interaction["rect"]) {
    if (!this.state.bug) return;
    const was = this.state.selected.includes(id);
    this.set({ selected: was ? this.state.selected.filter((x) => x !== id) : [...this.state.selected, id] });
    this.lastTapped = id;
    this.lastCtx = "";
    this.note({ kind: "tap", elementId: id, rect, selected: !was });
    if (!was) this.showPicked([id], 700); // short pulse so the selection is felt
    this.scheduleContext();
  }

  /** A drawn loop: select everything inside it. */
  selectMany(ids: string[], rect: Interaction["rect"], path: [number, number][]) {
    if (!this.state.bug) return;
    this.lastCircle = ids;
    this.set({ selected: [...new Set([...this.state.selected, ...ids])] });
    this.note({ kind: "circle", elementId: ids[0], elementIds: ids, rect, path });
  }

  setHover(id: string | null) {
    if (this.state.hover !== id) this.set({ hover: id });
  }

  /** Briefly outline elements (after a circle, or when a fix lands). */
  showPicked(ids: string[], ms = 1600) {
    const key = Date.now();
    this.set({ picked: { ids, key } });
    if (this.pickTimer) clearTimeout(this.pickTimer);
    this.pickTimer = window.setTimeout(() => {
      if (this.state.picked?.key === key) this.set({ picked: null });
    }, ms);
  }

  // ---- bug.fix: the coding agent's progress ------------------------------
  private onFix(msg: Msg) {
    const st = String(msg.state ?? "");
    if (!["queued", "working", "done", "failed"].includes(st)) return;
    const key = Date.now();
    const fix = { state: st as "queued" | "working" | "done" | "failed", summary: String(msg.summary ?? ""), key };
    this.set({ fix, flash: null });
    try {
      sessionStorage.setItem("pineo.fix", JSON.stringify(fix));
    } catch {
      /* ignore */
    }
    if (this.fixTimer) clearTimeout(this.fixTimer);
    if (st === "queued" || st === "working") {
      // never sit on "Working on it…" for ever: after 25 s say so calmly, then step aside
      this.fixTimer = window.setTimeout(() => {
        if (this.state.fix?.key !== key) return;
        this.set({ fix: { ...fix, slow: true } });
        this.fixTimer = window.setTimeout(() => this.state.fix?.key === key && this.set({ fix: null }), 12000);
      }, 25000);
    }
    if (st === "done" || st === "failed") {
      this.fixTimer = window.setTimeout(() => {
        if (this.state.fix?.key === key) this.set({ fix: null });
      }, 7000);
    }
    if (st === "done") {
      const ids = (Array.isArray(msg.elementIds) ? (msg.elementIds as string[]) : null) ?? this.touched.get(String(msg.bugId)) ?? [];
      // give hot reload a moment to swap the changed element in, then flash it
      const onScreen = ids.filter((id) => ELEMENT_BY_ID.get(id)?.screen !== "*"); // not the tab bar
      if (onScreen.length) setTimeout(() => this.showPicked(onScreen.slice(-6), 2600), 500); // only those on the current screen are drawn
    }
  }

  // ---- app.context -------------------------------------------------------
  private watchContext() {
    this.io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const rootH = e.rootBounds?.height ?? window.innerHeight;
          const shown =
            e.isIntersecting && (e.intersectionRatio >= 0.5 || e.intersectionRect.height >= rootH * 0.4);
          if (shown) this.visible.add(e.target);
          else this.visible.delete(e.target);
        }
        this.scheduleContext();
      },
      { threshold: [0, 0.25, 0.5, 0.75, 1] },
    );
    const rescan = () => {
      this.rescanQueued = false;
      const now = new Set(document.querySelectorAll("[data-pineo-id]"));
      for (const el of this.observed) {
        if (!now.has(el)) {
          this.io!.unobserve(el);
          this.observed.delete(el);
          this.visible.delete(el);
        }
      }
      for (const el of now) {
        if (!this.observed.has(el)) {
          this.observed.add(el);
          this.io!.observe(el);
        }
      }
    };
    new MutationObserver(() => {
      if (this.rescanQueued) return;
      this.rescanQueued = true;
      requestAnimationFrame(rescan);
    }).observe(document.body, { childList: true, subtree: true });
    rescan();

    document.addEventListener("scroll", () => this.scheduleContext(), { capture: true, passive: true });
    window.addEventListener("resize", () => this.scheduleContext());
    document.addEventListener(
      "click",
      (e) => {
        const target = e.target as Element | null;
        if (!target || target.closest(".pineo-root")) return;
        const el = target.closest("[data-pineo-id]");
        if (el) {
          this.lastTapped = el.getAttribute("data-pineo-id") ?? undefined;
          this.lastCtx = "";
        }
        this.scheduleContext();
      },
      true,
    );
    document.addEventListener(
      "change",
      (e) => {
        const el = (e.target as Element | null)?.closest?.("[data-pineo-id]");
        if (el && !(e.target as Element).closest(".pineo-root")) this.note({ kind: "input", elementId: el.getAttribute("data-pineo-id") ?? undefined });
      },
      true,
    );
    this.host.onScreenChange(() => {
      this.note({ kind: "screen" });
      this.lastCtx = "";
      this.scheduleContext(120);
    });
  }

  /** Debounced (scroll settle) with a 1 s ceiling so a long scroll still reports. */
  private scheduleContext(delay = 250) {
    const now = Date.now();
    if (this.ctxTimer) clearTimeout(this.ctxTimer);
    else this.ctxFirstAt = now;
    const wait = Math.max(0, Math.min(delay, this.ctxFirstAt + 1000 - now));
    this.ctxTimer = window.setTimeout(() => {
      this.ctxTimer = null;
      this.sendContext();
    }, wait);
  }

  private sendContext() {
    const ids: string[] = [];
    for (const el of this.visible) {
      const id = el.getAttribute("data-pineo-id");
      if (el.isConnected && id && !ids.includes(id)) ids.push(id);
    }
    const payload = {
      screen: this.host.getScreen(),
      visibleElementIds: ids,
      lastTappedElementId: this.lastTapped,
      viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio },
    };
    const key = JSON.stringify(payload);
    if (key === this.lastCtx) return;
    if (this.send("app.context", payload)) this.lastCtx = key;
  }

  // ---- assist ------------------------------------------------------------
  private showAssist(elementId: string | null, say: string) {
    const key = ++this.assistSeq;
    this.set({ assist: { key, elementId, say } });
    if (this.assistTimer) clearTimeout(this.assistTimer);
    const ms = Math.min(9000, Math.max(5000, 2500 + say.length * 55));
    this.assistTimer = window.setTimeout(() => {
      if (this.state.assist?.key === key) this.set({ assist: null });
    }, ms);
  }

  private async waitFor<T>(fn: () => T | null | undefined | false, timeout: number): Promise<T | null> {
    const end = Date.now() + timeout;
    for (;;) {
      const v = fn();
      if (v) return v;
      if (Date.now() > end) return null;
      await sleep(40);
    }
  }

  async runAssist(elementId: string, say: string) {
    const seq = this.assistSeq + 1;
    const meta = ELEMENT_BY_ID.get(elementId);
    this.log(`assist ${elementId}`);
    const fail = () => {
      this.showAssist(null, say || "I couldn't find that here.");
      this.send("app.assist.shown", { elementId, ok: false });
    };
    if (!meta) return fail();

    // 1. go to the element's screen
    if (meta.screen !== "*" && this.host.getScreen() !== meta.screen) this.host.navigate(meta.screen);
    const el = await this.waitFor(() => document.querySelector<HTMLElement>(sel(elementId)), 2500);
    if (!el) return fail();

    // 2. open anything that hides it (accordions etc. mark themselves with data-pineo-collapsed)
    for (let i = 0; i < 4; i++) {
      const collapsed = el.closest<HTMLElement>('[data-pineo-collapsed="true"]');
      if (!collapsed) break;
      collapsed.dispatchEvent(new CustomEvent("pineo:expand"));
      await this.waitFor(() => collapsed.getAttribute("data-pineo-collapsed") !== "true", 600);
    }
    await sleep(30);

    // 3. bring it to the middle of the view (walks every scroll container up the tree)
    el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
    let last = el.getBoundingClientRect().top;
    let still = 0;
    await this.waitFor(() => {
      const top = el.getBoundingClientRect().top;
      still = Math.abs(top - last) < 0.5 ? still + 1 : 0;
      last = top;
      return still >= 4;
    }, 2000);
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight || r.width === 0) {
      el.scrollIntoView({ behavior: "auto", block: "center", inline: "center" });
    }
    if (this.assistSeq + 1 !== seq) return; // a newer assist took over

    // 4. marker + bubble
    this.showAssist(elementId, say || meta.label);
    this.send("app.assist.shown", { elementId, ok: true });
  }

  // ---- debug helpers -----------------------------------------------------
  private watchTripleTap() {
    let taps: number[] = [];
    document.addEventListener(
      "pointerdown",
      (e) => {
        if (e.clientY > 64) return;
        const now = Date.now();
        taps = [...taps.filter((t) => now - t < 700), now];
        if (taps.length >= 3) {
          taps = [];
          this.set({ debugOpen: !this.state.debugOpen });
        }
      },
      true,
    );
  }

  /** Call a hub debug route; falls back to simulating the hub locally when it is unreachable. */
  async debug(route: "gesture" | "transcript" | "assist", body: Record<string, unknown>) {
    try {
      const res = await fetch(`/api/debug/${route}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.log(`debug ${route} → hub ok`);
    } catch (err) {
      this.log(`debug ${route} → local (${String(err instanceof Error ? err.message : err)})`);
      this.simulate(route, body);
    }
  }

  private simulate(route: string, body: Record<string, unknown>) {
    if (route === "gesture") {
      if (body.name === "double") {
        if (this.state.session) this.handle({ type: "session.ended", tickets: 0 });
        else this.handle({ type: "session.started", sessionId: `sess_local_${Date.now()}`, startedAt: Date.now() });
      } else if (body.name === "single") {
        if (this.state.bug) this.simulate("gesture", { name: "submit" });
        else if (this.state.session) this.handle({ type: "bug.opened", bugId: `bug_local_${Date.now()}` });
      } else if (body.name === "open") this.handle({ type: "bug.opened", bugId: `bug_local_${Date.now()}` });
      else if (body.name === "submit") {
        this.handle({ type: "bug.closing" });
        setTimeout(() => this.handle({ type: "bug.submitted" }), 600);
      }
      else this.handle({ type: "bug.cancelled" });
    } else if (route === "transcript") {
      this.handle({ type: "transcript", ...body });
    } else if (route === "assist") {
      this.handle({ type: body.elementId ? "assist" : "assist.none", ...body });
    }
  }
}
