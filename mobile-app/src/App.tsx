import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Icon } from "./ui";
import { Home } from "./screens/Home";
import { Cards } from "./screens/Cards";
import { Transfer } from "./screens/Transfer";
import { Activity } from "./screens/Activity";
import { Profile } from "./screens/Profile";
import { getScreen, navigate, onScreenChange, type Screen } from "./router";

const TITLES: Record<Screen, string> = {
  home: "Brindle",
  cards: "Cards",
  transfer: "Send money",
  activity: "Activity",
  profile: "Profile",
};

export function App() {
  const [screen, setScreen] = useState<Screen>(getScreen());
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => onScreenChange(() => setScreen(getScreen())), []);
  // Remember the scroll position per screen so a hot reload (or a full one) lands in the same place.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const key = `brindle.scroll.${screen}`;
    el.scrollTop = Number(sessionStorage.getItem(key) ?? 0);
    const save = () => sessionStorage.setItem(key, String(el.scrollTop));
    el.addEventListener("scroll", save, { passive: true });
    return () => el.removeEventListener("scroll", save);
  }, [screen]);

  return (
    <div className="app" data-screen={screen}>
      <header className="topbar">
        <div className="brand">
          {screen === "home" ? (
            <>
              <span className="logo" aria-hidden="true">
                <i />
              </span>
              <span className="brand-name">Brindle</span>
            </>
          ) : (
            <h1>{TITLES[screen]}</h1>
          )}
        </div>
        <button type="button" className="icon-btn" data-pineo-id="header-notifications" aria-label="Notifications" onClick={() => navigate("profile")}>
          <Icon name="bell" />
          <span className="badge">2</span>
        </button>
      </header>

      <main className="scroll" ref={scroller} key={screen}>
        {screen === "home" && <Home />}
        {screen === "cards" && <Cards />}
        {screen === "transfer" && <Transfer />}
        {screen === "activity" && <Activity />}
        {screen === "profile" && <Profile />}
      </main>

      <nav className="tabbar" aria-label="Main">
        <Tab id="home" icon="home" label="Home" current={screen} data-pineo-id="tab-home" />
        <Tab id="cards" icon="card" label="Cards" current={screen} data-pineo-id="tab-cards" />
        <button type="button" className={"tab tab-send" + (screen === "transfer" ? " active" : "")} data-pineo-id="tab-transfer" onClick={() => navigate("transfer")} aria-label="Send money">
          <span>
            <Icon name="send" size={22} />
          </span>
          <em>Send</em>
        </button>
        <Tab id="activity" icon="list" label="Activity" current={screen} data-pineo-id="tab-activity" />
        <Tab id="profile" icon="user" label="Profile" current={screen} data-pineo-id="tab-profile" />
      </nav>
    </div>
  );
}

/** One bottom tab. To restyle ONE tab, pass style={{…}} or className="…" where it is used. */
function Tab(p: { id: Screen; icon: string; label: string; current: Screen; "data-pineo-id": string; className?: string; style?: CSSProperties }) {
  return (
    <button type="button" className={"tab" + (p.current === p.id ? " active" : "") + (p.className ? " " + p.className : "")} style={p.style} data-pineo-id={p["data-pineo-id"]} aria-current={p.current === p.id ? "page" : undefined} onClick={() => navigate(p.id)}>
      <Icon name={p.icon} size={23} />
      <em>{p.label}</em>
    </button>
  );
}
