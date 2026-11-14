import "./Profile.css";
import { useEffect, useState } from "react";
import { USER } from "../data";
import { Accordion, Avatar, LinkRow, Toast, ToggleRow, useToast } from "../ui";

export function Profile() {
  const [toast, showToast] = useToast();
  const [bio, setBio] = useState(true);
  const [twoFa, setTwoFa] = useState(true);
  const [push, setPush] = useState(true);
  const [emailN, setEmailN] = useState(true);
  const [large, setLarge] = useState(true);
  const [marketing, setMarketing] = useState(true);
  const [dark, setDark] = useState(false);
  const [lang, setLang] = useState("English");

  // DEMO BUG 7: "Offers and news" cannot be switched off — a stray effect turns it back on.
  useEffect(() => {
    if (marketing) return;
    const t = setTimeout(() => setMarketing(true), 1200);
    return () => clearTimeout(t);
  }, [marketing]);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    return () => {
      document.documentElement.dataset.theme = "light";
    };
  }, [dark]);

  return (
    <div className="screen">
      <section className="me">
        <Avatar name={USER.name} color="var(--plum)" size={64} />
        <div>
          <b>{USER.name}</b>
          <small>
            {USER.tag} · Member since 2022
          </small>
        </div>
        <button type="button" className="btn btn-small btn-outline" data-pineo-id="profile-edit" onClick={() => showToast("Profile editing is not in the demo")}>
          Edit
        </button>
      </section>

      <h2 className="section-title">Account</h2>
      <div className="group">
        <Accordion data-pineo-id="profile-personal-details" icon="user" title="Personal details" sub="Email, phone, address">
          <LinkRow data-pineo-id="profile-email" title="Email" value={USER.email} onClick={() => showToast("We sent a confirmation link")} />
          <LinkRow data-pineo-id="profile-phone" title="Phone" value={USER.phone} onClick={() => showToast("We sent a code by text")} />
          <LinkRow data-pineo-id="profile-address" title="Home address" sub={USER.address} onClick={() => showToast("Address change needs proof of address")} />
        </Accordion>
        <Accordion data-pineo-id="profile-security" icon="shield" title="Security" sub="Passcode, Face ID, devices">
          <LinkRow data-pineo-id="profile-change-passcode" icon="lock" title="Change passcode" sub="The 6 digits you use to open the app" onClick={() => showToast("Passcode change is not in the demo")} />
          <ToggleRow data-pineo-id="profile-biometrics-toggle" title="Unlock with face or fingerprint" on={bio} onChange={setBio} />
          <ToggleRow data-pineo-id="profile-2fa-toggle" title="Two-step verification" sub="Ask for a code on new devices" on={twoFa} onChange={setTwoFa} />
          <LinkRow data-pineo-id="profile-devices" icon="device" title="Signed-in devices" value="2 devices" onClick={() => showToast("This phone · Tablet, last used Sunday")} />
        </Accordion>
        <Accordion data-pineo-id="profile-notifications" icon="bell" title="Notifications" sub="Push, email, payment alerts">
          <ToggleRow data-pineo-id="notif-push-toggle" title="Push notifications" sub="Every payment, in and out" on={push} onChange={setPush} />
          <ToggleRow data-pineo-id="notif-email-toggle" title="Email summaries" sub="A weekly round-up on Mondays" on={emailN} onChange={setEmailN} />
          <ToggleRow data-pineo-id="notif-large-payment-toggle" title="Large payment alerts" sub="Anything over $500" on={large} onChange={setLarge} />
          <ToggleRow data-pineo-id="notif-marketing-toggle" title="Offers and news" sub="Product news and partner offers" on={marketing} onChange={setMarketing} />
        </Accordion>
      </div>

      <h2 className="section-title">App</h2>
      <div className="group">
        <ToggleRow data-pineo-id="profile-dark-mode" icon="moon" title="Dark mode" sub="Easier on the eyes at night" on={dark} onChange={setDark} />
        <label className="row" data-pineo-id="profile-language">
          <span className="row-icon">
            <svg className="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" />
            </svg>
          </span>
          <div className="row-text">
            <b>Language</b>
          </div>
          <select className="inline-select" value={lang} onChange={(e) => setLang(e.target.value)}>
            <option>English</option>
            <option>Svenska</option>
            <option>Español</option>
            <option>Deutsch</option>
          </select>
        </label>
      </div>

      <h2 className="section-title">Documents</h2>
      <div className="group">
        <LinkRow data-pineo-id="profile-statements" icon="doc" title="Statements" sub="Monthly PDFs for every account" onClick={() => showToast("November statement is ready on the 1st")} />
        <LinkRow data-pineo-id="profile-fees-limits" icon="doc" title="Fees and limits" sub="What each plan costs and allows" onClick={() => showToast("Instant transfers cost $1.50")} />
        <LinkRow data-pineo-id="profile-tax-documents" icon="doc" title="Tax documents" sub="Annual interest certificate" onClick={() => showToast("Available in January")} />
      </div>

      <h2 className="section-title">Help</h2>
      <div className="group">
        <Accordion data-pineo-id="profile-support" icon="help" title="Help and support" sub="Chat, call, common questions">
          <LinkRow data-pineo-id="support-chat" icon="chat" title="Chat with us" sub="Usually replies in 2 minutes" onClick={() => showToast("Chat opens in the full app")} />
          <LinkRow data-pineo-id="support-call" icon="phone" title="Call support" sub="Every day, 07:00–22:00" onClick={() => showToast("Calling is not in the demo")} />
          <LinkRow data-pineo-id="support-faq" icon="help" title="Common questions" sub="Cards, transfers, fees" onClick={() => showToast("Help centre opens in the full app")} />
          <LinkRow data-pineo-id="support-report-problem" icon="flag" title="Report a problem" sub="Something not working as it should" onClick={() => showToast("Thanks — we'll look into it")} />
        </Accordion>
        <LinkRow data-pineo-id="profile-legal" icon="doc" title="Terms and privacy" onClick={() => showToast("Legal documents open in the full app")} />
      </div>

      <div className="stack">
        <button type="button" className="btn btn-outline" data-pineo-id="profile-logout" onClick={() => showToast("Signed out (not really — this is a demo)")}>
          Log out
        </button>
        <button type="button" className="link center danger" data-pineo-id="profile-close-account" onClick={() => showToast("Closing an account needs a call with support")}>
          Close my account
        </button>
        <p className="version" data-pineo-id="profile-app-version">
          Brindle 4.12.0 (build 2291)
        </p>
      </div>

      <Toast text={toast} />
    </div>
  );
}
