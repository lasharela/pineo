import "./Activity.css";
import { useMemo, useState, type CSSProperties } from "react";
import { TODAY, TRANSACTIONS, usd, type Tx } from "../data";
import { Avatar, Icon, Toast, useToast } from "../ui";

const KIND_COLOR: Record<string, string> = {
  card: "var(--lilac-strong)",
  transfer: "var(--teal)",
  income: "var(--green)",
  bill: "var(--tangerine)",
};

type Filter = "all" | "in" | "out" | "card" | "bill";

// DEMO BUG 6: one row carries a hard-coded date override ("31 Nov", a day that does not exist,
// listed under Yesterday). See `dateLabel` in data.ts.
const dateOf = (t: Tx) => t.dateLabel ?? `${t.day} ${t.month}`;

/** One payment row. To restyle ONE row, pass style={{…}} or className="…" where it is used. */
export function TxRow({ tx, plain, className, style }: { tx: Tx; plain?: boolean; className?: string; style?: CSSProperties }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={"tx" + (open ? " open" : "") + (className ? " " + className : "")} style={style} data-pineo-id={plain ? undefined : tx.pineoId}>
      <button type="button" className="row tx-main" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Avatar name={tx.who} color={KIND_COLOR[tx.kind]} size={40} />
        <div className="row-text">
          <b>{tx.who}</b>
          <small>
            {dateOf(tx)}, {tx.time} · {tx.what}
          </small>
        </div>
        <span className={"row-amount" + (tx.amount > 0 ? " pos" : "")}>{usd(tx.amount, true)}</span>
      </button>
      {open && (
        <div className="tx-detail">
          <div>
            <span>Reference</span>
            <span className="num">BRN-{tx.id.toUpperCase()}-{Math.abs(Math.round(tx.amount * 7))}</span>
          </div>
          <div>
            <span>Status</span>
            <span>Completed</span>
          </div>
        </div>
      )}
    </div>
  );
}

export function Activity() {
  const [filter, setFilter] = useState<Filter>("all");
  const [month, setMonth] = useState<"Nov" | "Oct" | "both">("both");
  const [query, setQuery] = useState("");
  const [toast, showToast] = useToast();

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = TRANSACTIONS.filter((t) => {
      if (month !== "both" && t.month !== month) return false;
      if (q && !`${t.who} ${t.what}`.toLowerCase().includes(q)) return false;
      if (filter === "in") return t.amount > 0;
      if (filter === "out") return t.amount < 0;
      if (filter === "card") return t.kind === "card";
      if (filter === "bill") return t.kind === "bill";
      return true;
    });
    const out: { title: string; items: Tx[] }[] = [];
    for (const t of list) {
      const title =
        t.month === TODAY.month && t.day === TODAY.day ? "Today" : t.month === TODAY.month && t.day === TODAY.day - 1 ? "Yesterday" : `${t.day} ${t.month === "Nov" ? "November" : "October"}`;
      const last = out[out.length - 1];
      if (last?.title === title) last.items.push(t);
      else out.push({ title, items: [t] });
    }
    return out;
  }, [filter, month, query]);

  const moneyIn = TRANSACTIONS.filter((t) => t.month === "Nov" && t.amount > 0).reduce((a, t) => a + t.amount, 0);
  const moneyOut = TRANSACTIONS.filter((t) => t.month === "Nov" && t.amount < 0).reduce((a, t) => a + t.amount, 0);

  return (
    <div className="screen">
      <label className="field search" data-pineo-id="activity-search">
        <Icon name="search" size={18} />
        <input placeholder="Search payments" value={query} onChange={(e) => setQuery(e.target.value)} />
      </label>

      <div className="chips-rail tight" data-pineo-id="activity-filters">
        <button type="button" className={"chip" + (filter === "all" ? " on" : "")} data-pineo-id="activity-filter-all" onClick={() => setFilter("all")}>
          All
        </button>
        <button type="button" className={"chip" + (filter === "in" ? " on" : "")} data-pineo-id="activity-filter-in" onClick={() => setFilter("in")}>
          Money in
        </button>
        <button type="button" className={"chip" + (filter === "out" ? " on" : "")} data-pineo-id="activity-filter-out" onClick={() => setFilter("out")}>
          Money out
        </button>
        <button type="button" className={"chip" + (filter === "card" ? " on" : "")} data-pineo-id="activity-filter-card" onClick={() => setFilter("card")}>
          Card
        </button>
        <button type="button" className={"chip" + (filter === "bill" ? " on" : "")} data-pineo-id="activity-filter-bills" onClick={() => setFilter("bill")}>
          Bills
        </button>
      </div>

      <div className="segmented" data-pineo-id="activity-month-picker" role="group" aria-label="Month">
        {(["both", "Nov", "Oct"] as const).map((m) => (
          <button type="button" key={m} className={month === m ? "on" : ""} onClick={() => setMonth(m)}>
            {m === "both" ? "Last 60 days" : m === "Nov" ? "November" : "October"}
          </button>
        ))}
      </div>

      <section className="inout" data-pineo-id="activity-month-totals">
        <div>
          <small>In · November</small>
          <b className="pos">{usd(moneyIn, true)}</b>
        </div>
        <div>
          <small>Out · November</small>
          <b>{usd(moneyOut)}</b>
        </div>
      </section>

      <div data-pineo-id="activity-list">
        {groups.map((g) => (
          <div key={g.title}>
            <h3 className="day">{g.title}</h3>
            <section className="group">
              {g.items.map((t) => (
                <TxRow key={t.id} tx={t} />
              ))}
            </section>
          </div>
        ))}
        {groups.length === 0 && <p className="empty">Nothing matches. Try another filter or search word.</p>}
      </div>

      <div className="stack">
        <button type="button" className="btn btn-outline" data-pineo-id="activity-load-older" onClick={() => showToast("That is everything from the last 60 days")}>
          Load older payments
        </button>
        <button type="button" className="btn" data-pineo-id="activity-export-statement" onClick={() => showToast("Statement saved to Files")}>
          <Icon name="download" size={18} /> Export statement (PDF)
        </button>
        <button type="button" className="link center" data-pineo-id="activity-dispute" onClick={() => showToast("A specialist will contact you")}>
          Don't recognise a payment? Dispute it
        </button>
      </div>

      <Toast text={toast} />
    </div>
  );
}
