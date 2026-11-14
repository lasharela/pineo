import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initPineo } from "./pineo";
import { getScreen, navigate, onScreenChange } from "./router";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

initPineo({ getScreen, navigate, onScreenChange });
