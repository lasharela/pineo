import { useEffect, useRef, useState } from "react";
import { ACCOUNTS, RECIPIENTS, usd } from "../data";
import { Avatar, Icon, ToggleRow } from "../ui";
import { navigate } from "../router";

const FEE_INSTANT = 1.5;

export function Transfer() {
  const [from, setFrom] = useState("everyday");
  const [query, setQuery] = useState("");
  const [name, setName] = useState("");
  const [iban, setIban] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [note, setNote] = useState("");
  const [instant, setInstant] = useState(true);
  const [scheduled, setScheduled] = useState(false);
  const [date, setDate] = useState("2026-11-21");
  const [repeat, setRepeat] = useState("never");
  const [save, setSave] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const scheduleFields = useRef<HTMLDivElement>(null);

  // Pineo asks hidden containers to open before it points at something inside them.
  useEffect(() => {
    const el = scheduleFields.current;
    if (!el) return;
    const expand = () => setScheduled(true);
    el.addEventListener("pineo:expand", expand);
    return () => el.removeEventListener("pineo:expand", expand);
  }, [sent]);

  // DEMO BUG 4: the amount is a free-text field and is never validated, so letters go straight through.
  const value = parseFloat(amount.replace(",", "."));
  const fee = instant ? FEE_INSTANT : 0;
  // DEMO BUG 5: the fee is subtracted instead of added, so the total is lower than the amount.
  const total = value - fee;

  const shown = RECIPIENTS.filter((r) => r.name.toLowerCase().includes(query.toLowerCase()));

  const submit = () => {
    if (!name.trim()) return setError("Add who you are sending to.");
    if (!confirm) return setError("Tick the box to confirm the details.");
    setError("");
    setSent(true);
  };

  if (sent) {
    return (
      <div className="screen sent">
        <div className="sent-mark">
          <Icon name="check" size={40} />
        </div>
        <h2>{amount ? usd(value) : "$0.00"} is on its way</h2>
        <p>
          To {name}
          {scheduled ? ` on ${date}` : instant ? " · arrives in seconds" : " · arrives next working day"}
        </p>
        <button type="button" className="btn" onClick={() => navigate("activity")}>
          View activity
        </button>
        <button type="button" className="btn btn-quiet" onClick={() => setSent(false)}>
          Send another
        </button>
      </div>
    );
  }

  return (
    <div className="screen form">
      <label className="field" data-pineo-id="transfer-from-account">
        <span>From</span>
        <select value={from} onChange={(e) => setFrom(e.target.value)}>
          {ACCOUNTS.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} · {usd(a.balance)}
            </option>
          ))}
        </select>
      </label>

      <h2 className="section-title">Who to</h2>
      <label className="field search" data-pineo-id="transfer-recipient-search">
        <Icon name="search" size={18} />
        <input placeholder="Search saved recipients" value={query} onChange={(e) => setQuery(e.target.value)} />
      </label>
      <div className="chips-rail" data-pineo-id="transfer-recent-recipients">
        {shown.map((r) => (
          <button type="button" key={r.name} className={"person" + (name === r.name ? " on" : "")} onClick={() => { setName(r.name); setIban(r.iban); }}>
            <Avatar name={r.name} color={r.color} size={46} />
            <em>{r.name.split(" ")[0]}</em>
          </button>
        ))}
        {shown.length === 0 && <small className="muted">No saved recipient matches.</small>}
      </div>
      <label className="field" data-pineo-id="transfer-recipient-name">
        <span>Recipient name</span>
        <input placeholder="Full name or business" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
      </label>
      <label className="field" data-pineo-id="transfer-iban">
        <span>Routing and account number</span>
        <input placeholder="021000021 · 000123456789" value={iban} onChange={(e) => setIban(e.target.value)} autoComplete="off" />
      </label>

      <h2 className="section-title">How much</h2>
      <div className="amount-row">
        <label className="field amount" data-pineo-id="transfer-amount">
          <span>Amount</span>
          <input placeholder="0.00" value={amount} onChange={(e) => setAmount(e.target.value)} autoComplete="off" />
        </label>
        <label className="field currency" data-pineo-id="transfer-currency">
          <span>Currency</span>
          <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
            <option>USD</option>
            <option>EUR</option>
            <option>GBP</option>
            <option>SEK</option>
          </select>
        </label>
      </div>
      <div className="amount-presets">
        {["20", "50", "100", "250"].map((v) => (
          <button type="button" key={v} className="chip" onClick={() => setAmount(v)}>
            ${v}
          </button>
        ))}
      </div>
      <label className="field" data-pineo-id="transfer-note">
        <span>Note for the recipient</span>
        <textarea rows={2} placeholder="e.g. Dinner on Friday" value={note} onChange={(e) => setNote(e.target.value)} />
      </label>

      <h2 className="section-title">When</h2>
      <section className="group">
        <ToggleRow data-pineo-id="transfer-instant-toggle" icon="send" title="Instant transfer" sub={`Arrives in seconds · fee ${usd(FEE_INSTANT)}`} on={instant} onChange={setInstant} />
        <ToggleRow data-pineo-id="transfer-schedule-toggle" icon="calendar" title="Schedule for later" sub="Pick the day the money leaves" on={scheduled} onChange={setScheduled} />
        {
          <div className="row row-fields" ref={scheduleFields} hidden={!scheduled} data-pineo-collapsed={scheduled ? "false" : "true"}>
            <label className="field" data-pineo-id="transfer-date">
              <span>Send on</span>
              <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            </label>
            <label className="field" data-pineo-id="transfer-repeat">
              <span>Repeat</span>
              <select value={repeat} onChange={(e) => setRepeat(e.target.value)}>
                <option value="never">Never</option>
                <option value="weekly">Every week</option>
                <option value="monthly">Every month</option>
              </select>
            </label>
          </div>
        }
        <ToggleRow data-pineo-id="transfer-save-recipient" icon="user" title="Save this recipient" sub="Find them faster next time" on={save} onChange={setSave} />
      </section>

      <h2 className="section-title">Summary</h2>
      <section className="card-block summary" data-pineo-id="transfer-summary">
        <div>
          <span>They receive</span>
          <span className="num">{amount ? usd(value) : "$0.00"}</span>
        </div>
        <div>
          <span>Transfer fee</span>
          <span className="num">{usd(fee)}</span>
        </div>
        <div className="summary-total">
          <b>Total leaving your account</b>
          <b className="num">{amount ? usd(total) : usd(0)}</b>
        </div>
      </section>

      <label className="check" data-pineo-id="transfer-confirm-checkbox">
        <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
        <span>I have checked the recipient and the amount. Transfers cannot be reversed.</span>
      </label>

      {error && <p className="form-error">{error}</p>}

      <button type="button" className="btn btn-big" data-pineo-id="transfer-submit" onClick={submit}>
        Submit transfer
      </button>
      <button type="button" className="btn btn-quiet" data-pineo-id="transfer-cancel" onClick={() => navigate("home")}>
        Cancel
      </button>
    </div>
  );
}
