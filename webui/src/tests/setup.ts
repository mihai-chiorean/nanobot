import "@testing-library/jest-dom/vitest";
import { Storage } from "happy-dom";
import { beforeEach } from "vitest";

import i18n from "@/i18n";

// Node 26 exposes an undefined experimental localStorage unless a backing file
// is configured. Ensure the DOM test environment remains the source of truth.
Object.defineProperty(globalThis, "localStorage", {
  value: new Storage(),
  configurable: true,
});

// happy-dom doesn't ship with ``crypto.randomUUID``; shim a tiny v4-ish helper.
if (!("randomUUID" in globalThis.crypto)) {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: () =>
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }),
    configurable: true,
  });
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.lang = "en";
  document.title = "nanobot";
  localStorage.setItem("nanobot.locale", "en");
});
