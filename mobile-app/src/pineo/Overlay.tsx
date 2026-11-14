import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ELEMENTS, LOADED_LABEL, type PineoClient } from "./client";

const LABELS = new Map(ELEMENTS.map((e) => [e.id, e.label]));

function useTick(active: boolean, ms: number) {
  const [, setN] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setN((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [active, ms]);
}

const fmt = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

export function Overlay({ client }: { client: PineoClient }) {
  const s = useSyncExternalStore(client.subscribe, client.getState);
  useTick(!!s.bug || !!s.session || s.captionAt > 0, 500);

  // captions stay on while closing; they step aside for "Ticket sent" / "Done" / failure lines
  const captionsLive = !!s.bug || s.closing || (!s.flash && !s.fix && !s.sessionEnded && Date.now() - s.captionAt < 4500);
  const hasCaption = captionsLive && (s.finals.length > 0 || !!s.interim);

  return (
    <>
      {s.bug && <div className="pineo-frame" />}

      {/* Session: calm blue hairline + chip at the top; the app stays fully usable. (A ticket adds the red frame and the dock below.) */}
      {s.session && <div className="pineo-session-line" />}
      <div className="pineo-top">
        {s.session ? (
          <div className="pineo-pill pineo-pill-session" role="status">
            <span className="pineo-session-dot on" />
            <span>Session</span>
            <span className="pineo-timer">{fmt(Date.now() - s.session.startedAt)}</span>
          </div>
        ) : s.band.connected && s.debugOpen ? (
          /* no session: nothing at all, except in debug where a tiny static dot says the band is connected */
          <span className="pineo-band-dot" aria-label="Band connected" />
        ) : null}
      </div>

      <div className="pineo-dock">
        {s.bug ? (
          <div className="pineo-pill pineo-pill-rec" role="status">
            <span className="pineo-dot" />
            <span>Listening… describe it</span>
            <span className="pineo-timer">{fmt(Date.now() - s.bug.startedAt)}</span>
            <Bars />
            {s.selected.length > 0 && (
              <span className="pineo-selected-line">
                Selected: {LABELS.get(s.selected[s.selected.length - 1]) ?? s.selected[s.selected.length - 1]}
                {s.selected.length > 1 ? ` +${s.selected.length - 1}` : ""}
              </span>
            )}
          </div>
        ) : s.closing ? (
          <div className="pineo-pill pineo-pill-closing" role="status">
            <span className="pineo-spin" />
            <span>Closing…</span>
          </div>
        ) : s.flash ? (
          <div className={`pineo-pill pineo-pill-${s.flash.kind}`} key={s.flash.key} role="status">
            {s.flash.kind === "submitted" ? (
              <>
                <Check />
                <span>Ticket sent</span>
              </>
            ) : (
              <span>Closed — nothing captured</span>
            )}
          </div>
        ) : s.sessionEnded ? (
          <div className="pineo-pill pineo-pill-session pineo-session-ended" key={s.sessionEnded.key} role="status">
            Session ended · {s.sessionEnded.tickets} {s.sessionEnded.tickets === 1 ? "ticket" : "tickets"}
          </div>
        ) : s.fix ? (
          <div className={`pineo-pill pineo-pill-fix pineo-fix-${s.fix.state}`} key={s.fix.state} role="status">
            {s.fix.state === "done" ? <Check /> : s.fix.state === "failed" ? <span className="pineo-warn">!</span> : <span className="pineo-spin" />}
            <span className="pineo-fix-text">
              {s.fix.state === "done"
                ? `Done: ${s.fix.summary || "change applied"}`
                : s.fix.state === "failed"
                  ? "Couldn't change that — see the dashboard"
                  : s.fix.slow
                    ? "Still working — see the dashboard"
                    : "Working on it…"}
            </span>
          </div>
        ) : null}
        {hasCaption ? (
          <div className="pineo-captions" aria-live="polite">
            <div className="pineo-captions-text">
              {s.finals.slice(-1).map((t, i) => (
                <span key={i} className="pineo-final">
                  {t}{" "}
                </span>
              ))}
              {s.interim && <span className="pineo-interim">{s.interim}</span>}
            </div>
          </div>
        ) : s.bug ? (
          <div className="pineo-hint">Tap or circle what you mean</div>
        ) : null}
      </div>

      {s.bug && <Marks ids={s.selected} hover={s.hover} />}
      {s.picked && s.picked.ids.map((id) => <Picked key={`${s.picked!.key}-${id}`} id={id} />)}
      {s.assist && <Assist key={s.assist.key} elementId={s.assist.elementId} say={s.assist.say} />}
      {s.debugOpen && <DebugPanel client={client} />}
    </>
  );
}

function Check() {
  return (
    <svg className="pineo-check" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <path d="M7 12.5l3.2 3.2L17 9" />
    </svg>
  );
}

/** Visible box of the nearest scrolling ancestor (the viewport if there is none). */
function scrollerRect(el: HTMLElement): { top: number; bottom: number } {
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    const o = getComputedStyle(p).overflowY;
    if ((o === "auto" || o === "scroll") && p.scrollHeight > p.clientHeight) {
      const r = p.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom };
    }
  }
  return { top: 0, bottom: window.innerHeight };
}

/** Selector mode: a solid outline + label chip on every selected element, a dashed one under the mouse. Follows scrolling. */
function Marks({ ids, hover }: { ids: string[]; hover: string | null }) {
  const box = useRef<HTMLDivElement>(null);
  const all = hover && !ids.includes(hover) ? [...ids, hover] : ids;
  useEffect(() => {
    let raf = 0;
    const place = () => {
      for (const node of box.current?.children ?? []) {
        const mark = node as HTMLElement;
        const el = document.querySelector<HTMLElement>(`[data-pineo-id="${CSS.escape(mark.dataset.pineoMark!)}"]`);
        const r = el?.getBoundingClientRect();
        if (!r || !r.width) {
          mark.style.opacity = "0";
          continue;
        }
        mark.style.opacity = "1";
        mark.style.transform = `translate(${r.left - 4}px, ${r.top - 4}px)`;
        mark.style.width = `${r.width + 8}px`;
        mark.style.height = `${r.height + 8}px`;
        // never paint over what the element itself is hidden behind (tab bar, header): clip to its scroller
        const c = scrollerRect(el!);
        const low = r.top - c.top < 30;
        const top = Math.max(c.top - (r.top - 4), low ? 0 : -22);
        const bottom = Math.max(r.bottom + 4 - c.bottom, low ? -22 : 0);
        mark.style.clipPath = `inset(${top}px -300px ${bottom}px -300px)`;
        mark.classList.toggle("pineo-mark-low", low);
        // keep the label inside the viewport when the element is near the right edge
        const chip = mark.firstElementChild as HTMLElement | null;
        if (chip) chip.style.marginLeft = `${Math.min(0, window.innerWidth - 8 - (r.left + 4 + chip.offsetWidth))}px`;
      }
      raf = requestAnimationFrame(place);
    };
    raf = requestAnimationFrame(place);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div ref={box}>
      {all.map((id) => (
        <div key={id} className={"pineo-mark" + (ids.includes(id) ? " pineo-mark-on" : " pineo-mark-hover")} data-pineo-mark={id}>
          <span>{LABELS.get(id) ?? id}</span>
        </div>
      ))}
    </div>
  );
}

/** Outline around an element that was circled (or just changed by a fix). */
function Picked({ id }: { id: string }) {
  const el = document.querySelector<HTMLElement>(`[data-pineo-id="${CSS.escape(id)}"]`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (!r.width) return null;
  return <div className="pineo-picked" data-pineo-picked={id} style={{ left: r.left - 5, top: r.top - 5, width: r.width + 10, height: r.height + 10 }} />;
}

function Bars() {
  return (
    <span className="pineo-bars" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

/** Halo around the element, a pulsing marker beside it and a speech bubble; follows the element while shown. */
function Assist({ elementId, say }: { elementId: string | null; say: string }) {
  const halo = useRef<HTMLDivElement>(null);
  const marker = useRef<HTMLDivElement>(null);
  const bubble = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let raf = 0;
    const place = () => {
      const b = bubble.current;
      const el = elementId
        ? document.querySelector<HTMLElement>(`[data-pineo-id="${CSS.escape(elementId)}"]`)
        : null;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      if (el && halo.current && marker.current && b) {
        const r = el.getBoundingClientRect();
        const pad = 6;
        const top = Math.max(4, r.top - pad);
        const left = Math.max(4, r.left - pad);
        const width = Math.min(vw - 4, r.right + pad) - left;
        const height = Math.min(vh - 4, r.bottom + pad) - top;
        Object.assign(halo.current.style, {
          transform: `translate(${left}px, ${top}px)`,
          width: `${width}px`,
          height: `${height}px`,
          opacity: "1",
        });
        // marker sits just outside the top-right corner, or inside it when there is no room
        const mx = Math.min(vw - 30, left + width - 10);
        const my = Math.max(6, top - 12);
        Object.assign(marker.current.style, { transform: `translate(${mx}px, ${my}px)`, opacity: "1" });
        const bh = b.offsetHeight;
        const bw = b.offsetWidth;
        const above = top - bh - 18;
        const by = above > 56 ? above : Math.min(vh - bh - 12, top + height + 16);
        const bx = Math.max(12, Math.min(vw - bw - 12, r.left + r.width / 2 - bw / 2));
        Object.assign(b.style, { transform: `translate(${bx}px, ${by}px)`, opacity: "1" });
        b.dataset.side = above > 56 ? "above" : "below";
        b.style.setProperty("--tail", `${Math.max(18, Math.min(bw - 18, r.left + r.width / 2 - bx))}px`);
      } else if (b) {
        const bx = (vw - b.offsetWidth) / 2;
        Object.assign(b.style, { transform: `translate(${bx}px, ${vh * 0.36}px)`, opacity: "1" });
        b.dataset.side = "none";
      }
      raf = requestAnimationFrame(place);
    };
    raf = requestAnimationFrame(place);
    return () => cancelAnimationFrame(raf);
  }, [elementId]);

  return (
    <>
      {elementId && (
        <>
          <div ref={halo} className="pineo-halo" data-pineo-assist={elementId} />
          <div ref={marker} className="pineo-marker">
            <span />
          </div>
        </>
      )}
      <div ref={bubble} className="pineo-bubble" role="status">
        {say}
      </div>
    </>
  );
}

function DebugPanel({ client }: { client: PineoClient }) {
  const s = useSyncExternalStore(client.subscribe, client.getState);
  const [text, setText] = useState("The total balance does not add up");
  const [target, setTarget] = useState("transfer-submit");
  const [say, setSay] = useState("Here it is.");
  return (
    <div className="pineo-debug">
      <div className="pineo-debug-head">
        <b>Pineo debug</b>
        <em className="pineo-build">code {LOADED_LABEL}</em>
        <span className={s.connected ? "ok" : "bad"}>{s.connected ? "hub connected" : "hub offline · local"}</span>
        <button onClick={() => client.setDebugOpen(false)} aria-label="Close debug panel">
          ✕
        </button>
      </div>
      <div className="pineo-debug-row">
        <button onClick={() => client.debug("gesture", { name: "double" })}>Double</button>
        <button onClick={() => client.debug("gesture", { name: "single" })}>Single</button>
      </div>
      <div className="pineo-debug-row">
        <button onClick={() => client.debug("gesture", { name: "open" })}>Open ticket</button>
        <button onClick={() => client.debug("gesture", { name: "submit" })}>Submit</button>
        <button onClick={() => client.debug("gesture", { name: "cancel" })}>Cancel</button>
      </div>
      <div className="pineo-debug-row">
        <input value={text} onChange={(e) => setText(e.target.value)} aria-label="Transcript text" />
        <button onClick={() => client.debug("transcript", { text, final: true })}>Say</button>
      </div>
      <div className="pineo-debug-row">
        <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Element">
          {ELEMENTS.map((e) => (
            <option key={e.id} value={e.id}>
              {e.screen} · {e.id}
            </option>
          ))}
        </select>
        <button onClick={() => client.debug("assist", { elementId: target, say })}>Assist</button>
      </div>
      <div className="pineo-debug-row">
        <input value={say} onChange={(e) => setSay(e.target.value)} aria-label="Assist text" />
      </div>
      <div className="pineo-debug-diag">{client.lastDiag || "touch the screen: the last raw event shows here"}</div>
      <pre className="pineo-debug-log">{s.log.slice(0, 5).join("\n") || "no events yet"}</pre>
    </div>
  );
}
