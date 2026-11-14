import "./Cards.css";
import { useRef, useState } from "react";
import { CARDS, usd } from "../data";
import { Icon, LinkRow, Toast, ToggleRow, useToast } from "../ui";

type Settings = { blocked: boolean; online: boolean; contactless: boolean; atm: boolean; abroad: boolean; daily: number; atmLimit: number };
const initial = (): Settings => ({ blocked: false, online: true, contactless: true, atm: true, abroad: false, daily: 1500, atmLimit: 300 });

export function Cards() {
  const [index, setIndex] = useState(0);
  const [settings, setSettings] = useState<Settings[]>(() => CARDS.map(initial));
  const [showPin, setShowPin] = useState(false);
  const [showNumber, setShowNumber] = useState(false);
  const [toast, showToast] = useToast();
  const rail = useRef<HTMLDivElement>(null);
  const pinTimer = useRef<number | undefined>(undefined);

  const card = CARDS[index];
  const s = settings[index];
  const set = (patch: Partial<Settings>) => setSettings((all) => all.map((v, i) => (i === index ? { ...v, ...patch } : v)));

  const onScroll = () => {
    const el = rail.current!;
    const step = el.scrollWidth / CARDS.length;
    const i = Math.max(0, Math.min(CARDS.length - 1, Math.round(el.scrollLeft / step)));
    if (i !== index) {
      setIndex(i);
      setShowPin(false);
      setShowNumber(false);
    }
  };

  const revealPin = () => {
    setShowPin(true);
    clearTimeout(pinTimer.current);
    pinTimer.current = window.setTimeout(() => setShowPin(false), 6000);
  };

  return (
    <div className="screen">
      <div className="card-rail" ref={rail} onScroll={onScroll} data-pineo-id="cards-carousel">
        {CARDS.map((c, i) => (
          <div className={`paycard tone-${c.tone}` + (settings[i].blocked ? " frozen" : "")} key={c.id}>
            <div className="paycard-top">
              <span className="logo small" aria-hidden="true">
                <i />
              </span>
              <span>{c.name}</span>
            </div>
            <div className="paycard-number">{showNumber && i === index ? c.full : `••••  ••••  ••••  ${c.last4}`}</div>
            <div className="paycard-bottom">
              <span>NOA LINDQVIST</span>
              <span>{c.exp}</span>
            </div>
            {settings[i].blocked && (
              <div className="frozen-tag">
                <Icon name="snow" size={16} /> Blocked
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="dots" aria-hidden="true">
        {CARDS.map((c, i) => (
          <i key={c.id} className={i === index ? "on" : ""} />
        ))}
      </div>

      <div className="quick quick-3">
        <button type="button" data-pineo-id="card-show-pin" onClick={revealPin}>
          <span>
            <Icon name="pin" />
          </span>
          {showPin ? <b className="pin-digits">{card.pin}</b> : "Show PIN"}
        </button>
        <button type="button" data-pineo-id="card-show-details" onClick={() => setShowNumber((v) => !v)}>
          <span>
            <Icon name={showNumber ? "eyeOff" : "eye"} />
          </span>
          {showNumber ? "Hide number" : "Card number"}
        </button>
        <button type="button" data-pineo-id="card-copy-number" onClick={() => showToast("Card number copied")}>
          <span>
            <Icon name="copy" />
          </span>
          Copy
        </button>
      </div>

      <h2 className="section-title">Security</h2>
      <section className="group">
        {/* DEMO BUG 3: the status line under "Block card" says the opposite of the switch */}
        <ToggleRow
          data-pineo-id="card-block-toggle"
          icon="lock"
          title="Block card"
          className="card-green-toggle"
          sub={s.blocked ? "Your card is active and ready to use" : "Card blocked — payments will be declined"}
          on={s.blocked}
          onChange={(v) => set({ blocked: v })}
        />
        <ToggleRow data-pineo-id="card-online-toggle" icon="globe" title="Online payments" sub="Shops and subscriptions on the web" on={s.online} onChange={(v) => set({ online: v })} className="card-green-toggle" />
        <ToggleRow data-pineo-id="card-contactless-toggle" icon="card" title="Contactless" sub="Tap to pay up to $50 without PIN" on={s.contactless} onChange={(v) => set({ contactless: v })} className="card-green-toggle" />
        <ToggleRow data-pineo-id="card-atm-toggle" icon="wallet" title="Cash withdrawals" sub="Allow this card at cash machines" on={s.atm} onChange={(v) => set({ atm: v })} className="card-green-toggle" />
        <ToggleRow data-pineo-id="card-abroad-toggle" icon="flag" title="Payments abroad" sub="Use the card outside your home country" on={s.abroad} onChange={(v) => set({ abroad: v })} className="card-green-toggle" />
      </section>

      <h2 className="section-title">Limits</h2>
      <section className="card-block">
        <label className="limit" data-pineo-id="card-limit-daily">
          <div>
            <b>Daily spending limit</b>
            <span className="num">{usd(s.daily)}</span>
          </div>
          <input type="range" min={100} max={5000} step={100} value={s.daily} onChange={(e) => set({ daily: Number(e.target.value) })} />
          <small>$318.40 spent today</small>
        </label>
        <label className="limit" data-pineo-id="card-limit-atm">
          <div>
            <b>Cash withdrawal limit</b>
            <span className="num">{usd(s.atmLimit)}</span>
          </div>
          <input type="range" min={50} max={1000} step={50} value={s.atmLimit} onChange={(e) => set({ atmLimit: Number(e.target.value) })} />
          <small>Per day, at any cash machine</small>
        </label>
      </section>

      <h2 className="section-title">Manage</h2>
      <section className="group">
        <LinkRow data-pineo-id="card-change-pin" icon="pin" title="Change PIN" sub="Pick a new 4-digit PIN" onClick={() => showToast("PIN change starts at a cash machine")} />
        <LinkRow data-pineo-id="card-add-to-wallet" icon="wallet" title="Add to phone wallet" sub="Pay with your phone or watch" onClick={() => showToast("Added to wallet")} />
        <LinkRow data-pineo-id="card-replace" icon="refresh" title="Replace card" sub="Damaged or expiring soon" onClick={() => showToast("Replacement ordered")} />
        <LinkRow data-pineo-id="card-order-new" icon="plus" title="Order another card" sub="Physical or virtual" onClick={() => showToast("We'll be in touch")} />
        <LinkRow data-pineo-id="card-report-lost" icon="shield" title="Report lost or stolen" sub="Blocks the card and sends a new one" danger onClick={() => { set({ blocked: true }); showToast("Card blocked, new one on the way"); }} />
      </section>

      <Toast text={toast} />
    </div>
  );
}
