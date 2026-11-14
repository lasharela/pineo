// Tiny hash router so the app (and Pineo) can navigate programmatically.
export const SCREENS = ["home", "cards", "transfer", "activity", "profile"] as const;
export type Screen = (typeof SCREENS)[number];

export function getScreen(): Screen {
  const name = location.hash.replace(/^#\/?/, "");
  return (SCREENS as readonly string[]).includes(name) ? (name as Screen) : "home";
}

export function navigate(screen: string) {
  if (!(SCREENS as readonly string[]).includes(screen)) return;
  if (getScreen() === screen) return;
  sessionStorage.removeItem(`brindle.scroll.${screen}`); // a fresh visit starts at the top
  location.hash = `/${screen}`;
}

export function onScreenChange(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}
