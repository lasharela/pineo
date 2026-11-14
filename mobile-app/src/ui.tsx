import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

const PATHS: Record<string, string> = {
  home: "M4 11.5 12 4l8 7.5M6.5 10v9.5h11V10",
  card: "M3 7.5A2.5 2.5 0 0 1 5.5 5h13A2.5 2.5 0 0 1 21 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5zM3 10h18M7 15h3",
  send: "M20.5 3.5 10 14M20.5 3.5 14 20.5l-4-6.5-6.5-4z",
  list: "M8 6.5h12M8 12h12M8 17.5h12M4 6.5h.01M4 12h.01M4 17.5h.01",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 20a7.5 7.5 0 0 1 15 0",
  bell: "M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 2h-15zM10 20.5a2 2 0 0 0 4 0",
  eye: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  eyeOff: "M4 4l16 16M9.9 5.8A8.6 8.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-2.6 3.4M6.3 7.6A16.500 16.500 0 0 0 2.500 12S6 18.500 12 18.500a8.700 8.700 0 0 0 3.600-.800",
  chevron: "M9 5.5 15.500 12 9 18.500",
  down: "M5.500 9 12 15.500 18.500 9",
  plus: "M12 5v14M5 12h14",
  request: "M12 4v12M6.500 10.500 12 16l5.500-5.500M5 20h14",
  qr: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h2.500v2.500H14zM17.500 17.500H20V20h-2.500zM14 20h.01M20 14h.01",
  lock: "M6.500 11V8a5.500 5.500 0 0 1 11 0v3M5 11h14v9.500H5z",
  snow: "M12 3v18M4.200 7.500l15.600 9M19.800 7.500l-15.600 9",
  search: "M10.500 17a6.500 6.500 0 1 0 0-13 6.500 6.500 0 0 0 0 13zM15.500 15.500 20.500 20.500",
  download: "M12 4v11M7 10.500l5 5 5-5M5 20h14",
  copy: "M9 9h10.500v10.500H9zM5 15H4.500V4.500H15V5",
  chat: "M4.500 5h15v11h-8l-4.500 4v-4H4.500z",
  phone: "M5 4.500h4l1.500 4.500-2 1.500a11 11 0 0 0 5 5l1.500-2 4.500 1.500v4A2 2 0 0 1 17.500 21 14.500 14.500 0 0 1 3 6.500a2 2 0 0 1 2-2z",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.500 9.500a2.500 2.500 0 1 1 3.500 2.300c-.700.400-1 1-1 1.700M12 16.800h.01",
  shield: "M12 3.500 19.500 6v6c0 4.500-3 7.500-7.500 9-4.500-1.500-7.500-4.500-7.500-9V6z",
  doc: "M7 3.500h7l4 4V20.500H7zM14 3.500v4h4M9.500 12.500h6M9.500 16h6",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c2.500 2.500 3.500 5.500 3.500 9s-1 6.500-3.500 9c-2.500-2.500-3.500-5.500-3.500-9s1-6.500 3.500-9z",
  moon: "M20 14.500A8.500 8.500 0 0 1 9.500 4a8.500 8.500 0 1 0 10.500 10.500z",
  logout: "M14 4.500h5.500v15H14M10 8l-4 4 4 4M6 12h9.500",
  flag: "M5.500 21V4M5.500 4.500h12l-2 4 2 4h-12",
  wallet: "M4 7.500A2.500 2.500 0 0 1 6.500 5H18v3M4 7.500V17a2.500 2.500 0 0 0 2.500 2.500H20V8H6.500A2.500 2.500 0 0 1 4 7.500zM16 14h.01",
  refresh: "M19.500 12a7.500 7.500 0 1 1-2.200-5.300M19.500 4v4h-4",
  gift: "M4 10h16v10.500H4zM3 6.500h18V10H3zM12 6.500v14M12 6.500S10.500 3 8.500 3a1.750 1.750 0 0 0 0 3.500zM12 6.500S13.500 3 15.500 3a1.750 1.750 0 0 1 0 3.500z",
  pin: "M5 9.500h14v8H5zM8.500 13.500h.01M12 13.500h.01M15.500 13.500h.01",
  calendar: "M4.500 6h15v14h-15zM4.500 10h15M8.500 3.500v4M15.500 3.500v4",
  check: "M5 12.500 10 17.500 19 7.500",
  x: "M6 6l12 12M18 6 6 18",
  trash: "M5 7h14M9.500 7V4.500h5V7M7 7l1 13h8l1-13",
  device: "M7.500 3.500h9v17h-9zM11 17.500h2",
};

export function Icon({ name, size = 22 }: { name: string; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] ?? PATHS.help} />
    </svg>
  );
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className={"toggle" + (on ? " on" : "")} onClick={() => onChange(!on)}>
      <span />
    </button>
  );
}

/** A settings row with a switch on the right. The whole row is the Pineo element.
 *  To restyle ONE row, pass style={{…}} or className="…" where it is used; that reaches the row's root element. */
export function ToggleRow(p: { "data-pineo-id": string; title: string; sub?: ReactNode; on: boolean; onChange: (v: boolean) => void; icon?: string; className?: string; style?: CSSProperties }) {
  return (
    <div className={"row" + (p.className ? " " + p.className : "")} style={p.style} data-pineo-id={p["data-pineo-id"]}>
      {p.icon && (
        <span className="row-icon">
          <Icon name={p.icon} size={20} />
        </span>
      )}
      <div className="row-text">
        <b>{p.title}</b>
        {p.sub && <small>{p.sub}</small>}
      </div>
      <Toggle on={p.on} onChange={p.onChange} label={p.title} />
    </div>
  );
}

/** A tappable row with a chevron (or a custom trailing value). */
export function LinkRow(p: { "data-pineo-id": string; title: string; sub?: ReactNode; icon?: string; value?: ReactNode; danger?: boolean; onClick?: () => void; className?: string; style?: CSSProperties }) {
  return (
    <button type="button" className={"row row-link" + (p.danger ? " danger" : "") + (p.className ? " " + p.className : "")} style={p.style} data-pineo-id={p["data-pineo-id"]} onClick={p.onClick}>
      {p.icon && (
        <span className="row-icon">
          <Icon name={p.icon} size={20} />
        </span>
      )}
      <div className="row-text">
        <b>{p.title}</b>
        {p.sub && <small>{p.sub}</small>}
      </div>
      {p.value && <span className="row-value">{p.value}</span>}
      <span className="row-chev">
        <Icon name="chevron" size={18} />
      </span>
    </button>
  );
}

/**
 * Collapsible section. While closed its body carries data-pineo-collapsed="true";
 * Pineo fires "pineo:expand" on the body to open it before pointing at something inside.
 */
export function Accordion(p: { "data-pineo-id": string; title: string; sub?: string; icon: string; children: ReactNode; defaultOpen?: boolean; className?: string; style?: CSSProperties }) {
  const [open, setOpen] = useState(!!p.defaultOpen);
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = body.current!;
    const expand = () => setOpen(true);
    el.addEventListener("pineo:expand", expand);
    return () => el.removeEventListener("pineo:expand", expand);
  }, []);
  return (
    <section className={"acc" + (open ? " open" : "")}>
      <button type="button" className={"row acc-head" + (p.className ? " " + p.className : "")} style={p.style} data-pineo-id={p["data-pineo-id"]} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="row-icon">
          <Icon name={p.icon} size={20} />
        </span>
        <div className="row-text">
          <b>{p.title}</b>
          {p.sub && <small>{p.sub}</small>}
        </div>
        <span className="row-chev acc-chev">
          <Icon name="down" size={18} />
        </span>
      </button>
      <div ref={body} className="acc-body" data-pineo-collapsed={open ? "false" : "true"} hidden={!open}>
        {p.children}
      </div>
    </section>
  );
}

export function Avatar({ name, color, size = 40 }: { name: string; color?: string; size?: number }) {
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join("")
    .toUpperCase();
  return (
    <span className="avatar" style={{ width: size, height: size, background: color, fontSize: size * 0.36 }}>
      {initials}
    </span>
  );
}

export function Toast({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <div className="toast" role="status">
      {text}
    </div>
  );
}

export function useToast(): [string | null, (t: string) => void] {
  const [text, setText] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = (t: string) => {
    setText(t);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setText(null), 2200);
  };
  return [text, show];
}
