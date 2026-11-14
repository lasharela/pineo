import "./Home.css";
import { useState } from "react";
import { ACCOUNTS, TRANSACTIONS, USER, usd } from "../data";
import { Avatar, Icon, Toast, useToast } from "../ui";
import { navigate } from "../router";
import { TxRow } from "./Activity";

// DEMO BUG 1: the headline total is a typo'd constant, not the sum of the accounts below
// (4,280.55 + 12,940.00 + 615.20 = 17,835.75).
const TOTAL_BALANCE = 17385.75;

const SPEND = [
  { label: "Rent & bills", amount: 1268.29, pct: 100, color: "var(--plum)" },
  { label: "Groceries", amount: 100.6, pct: 34, color: "var(--tangerine)" },
  { label: "Eating out", amount: 76.65, pct: 27, color: "var(--teal)" },
  { label: "Everything else", amount: 198.85, pct: 46, color: "var(--lilac-strong)" },
];

export function Home() {
  const [hidden, setHidden] = useState(false);
  const [toast, showToast] = useToast();
  const mask = (v: string) => (hidden ? "$ ••••••" : v);

  return (
    <div className="screen">
      <p className="hello">Good morning, {USER.first}</p>

      <section className="balance" data-pineo-id="home-balance" style={{ background: "#fff", color: "#000" }}>
        <div className="balance-top">
          <span>Total balance</span>
          <button type="button" className="ghost-icon" data-pineo-id="home-balance-hide" aria-label={hidden ? "Show balances" : "Hide balances"} onClick={() => setHidden((h) => !h)}>
            <Icon name={hidden ? "eyeOff" : "eye"} size={20} />
          </button>
        </div>
        <strong className="balance-figure" style={{ color: "var(--teal)" }}>{mask(usd(TOTAL_BALANCE))}</strong>
        <span className="balance-delta" style={{ color: "#000" }}>▲ $412.30 this month</span>
      </section>

      <div className="quick">
        <button type="button" data-pineo-id="home-quick-send" onClick={() => navigate("transfer")}>
          <span>
            <Icon name="send" />
          </span>
          Send
        </button>
        <button type="button" data-pineo-id="home-quick-request" onClick={() => showToast("Request link copied")}>
          <span>
            <Icon name="request" />
          </span>
          Request
        </button>
        <button type="button" data-pineo-id="home-quick-topup" onClick={() => showToast("Top-up is not available in the demo")}>
          <span>
            <Icon name="plus" />
          </span>
          Top up
        </button>
        <button type="button" data-pineo-id="home-quick-scan" onClick={() => showToast("Camera is not available in the demo")}>
          <span>
            <Icon name="qr" />
          </span>
          Scan
        </button>
      </div>

      <h2 className="section-title">Accounts</h2>
      <section className="group" data-pineo-id="home-accounts">
        <button type="button" className="row row-link" data-pineo-id="home-account-everyday" onClick={() => navigate("activity")}>
          <span className="acct-dot tone-plum" />
          <div className="row-text">
            <b>Everyday</b>
            <small>Main account ·· 4821</small>
          </div>
          <span className="row-amount">{mask(usd(ACCOUNTS[0].balance))}</span>
        </button>
        <button type="button" className="row row-link" data-pineo-id="home-account-savings" onClick={() => navigate("activity")}>
          <span className="acct-dot tone-teal" />
          <div className="row-text">
            <b>Rainy day</b>
            <small>Savings · 3.1% APY</small>
          </div>
          <span className="row-amount">{mask(usd(ACCOUNTS[1].balance))}</span>
        </button>
        <button type="button" className="row row-link" data-pineo-id="home-account-travel" onClick={() => navigate("activity")}>
          <span className="acct-dot tone-tangerine" />
          <div className="row-text">
            <b>Lisbon trip</b>
            <small>Pot · goal $1,500</small>
          </div>
          <span className="row-amount">{mask(usd(ACCOUNTS[2].balance))}</span>
        </button>
      </section>

      <h2 className="section-title">Savings goal</h2>
      {/* DEMO BUG 2: the button is absolutely positioned and sits on top of the goal text at phone width */}
      <section className="goal" data-pineo-id="home-savings-goal">
        <div className="goal-text">
          <b>Lisbon trip · $615.20 saved of $1,500.00</b>
          <small>You are 41% of the way there. Keep going until 30 March to stay on track.</small>
        </div>
        <button type="button" className="btn btn-small goal-add" data-pineo-id="home-goal-add" onClick={() => showToast("Added $25 to Lisbon trip")}>
          Add money
        </button>
        <div className="progress">
          <i style={{ width: "41%" }} />
        </div>
      </section>

      <h2 className="section-title">Spending in November</h2>
      <section className="card-block" data-pineo-id="home-spending-summary">
        <div className="spend-head">
          <strong>$1,644.39</strong>
          <small>$212 less than October</small>
        </div>
        {SPEND.map((s) => (
          <div className="spend-row" key={s.label}>
            <span>{s.label}</span>
            <div className="bar">
              <i style={{ width: `${s.pct}%`, background: s.color }} />
            </div>
            <span className="num">{usd(s.amount)}</span>
          </div>
        ))}
      </section>

      <div className="section-head">
        <h2 className="section-title">Recent activity</h2>
        <button type="button" className="link" data-pineo-id="home-see-all-activity" onClick={() => navigate("activity")}>
          See all
        </button>
      </div>
      <section className="group" data-pineo-id="home-recent-transactions">
        {TRANSACTIONS.slice(0, 5).map((t) => (
          <TxRow key={t.id} tx={t} plain />
        ))}
      </section>

      <button type="button" className="invite" data-pineo-id="home-invite-friends" onClick={() => showToast("Invite link copied")}>
        <Avatar name="+ 1" color="var(--tangerine)" size={44} />
        <div>
          <b>Invite a friend, you both get $20</b>
          <small>Share your link {USER.tag}</small>
        </div>
        <Icon name="gift" />
      </button>

      <Toast text={toast} />
    </div>
  );
}
