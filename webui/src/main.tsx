import { ClerkProvider } from "@clerk/react";
import { shadcn } from "@clerk/ui/themes";
import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import "./globals.css";
import "./i18n";

const root = document.getElementById("root");
if (!root) throw new Error("root element missing");
const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
if (!publishableKey) throw new Error("VITE_CLERK_PUBLISHABLE_KEY is missing");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <ClerkProvider
      publishableKey={publishableKey}
      afterSignOutUrl="/"
      appearance={{ theme: shadcn }}
    >
      <App />
    </ClerkProvider>
  </React.StrictMode>,
);
