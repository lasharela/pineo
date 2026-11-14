// Drop-in entry point: `initPineo(host)` connects to the hub and mounts the overlay in its own root.
// The host app only has to (1) put data-pineo-id on its elements, (2) list them in elements.json,
// (3) mark collapsed containers with data-pineo-collapsed="true" and open on the "pineo:expand" event.
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { PineoClient, type PineoHost } from "./client";
import { Overlay } from "./Overlay";
import { watchCircles } from "./circle";
import "./pineo.css";

export type { PineoHost, PineoElement } from "./client";
export { ELEMENTS } from "./client";

type W = { __pineo?: PineoClient };

/**
 * Safe to call again after a hot reload: the client, its socket and the overlay are created once
 * per page and kept on `window`, so listeners and overlays are never duplicated.
 */
export function initPineo(host: PineoHost): PineoClient {
  const w = window as unknown as W;
  if (w.__pineo) return w.__pineo;
  const client = new PineoClient(host);
  w.__pineo = client;
  const root = document.createElement("div");
  root.className = "pineo-root";
  document.body.appendChild(root);
  createRoot(root).render(createElement(Overlay, { client }));
  watchCircles(client); // registered before the client so a finished stroke can swallow its click
  client.start();
  return client;
}
