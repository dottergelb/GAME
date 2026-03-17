import React from "react";
import ReactDOM from "react-dom/client";
import { SDKProvider } from "@telegram-apps/sdk-react";
import { AppRoot } from "@telegram-apps/telegram-ui";
import "@telegram-apps/telegram-ui/dist/styles.css";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <SDKProvider acceptCustomStyles>
      <AppRoot appearance="auto" platform="base">
        <App />
      </AppRoot>
    </SDKProvider>
  </React.StrictMode>
);