import React from "react";
import { createRoot } from "react-dom/client";
import { DeskApp } from "./DeskApp.jsx";

const path = location.pathname.replace(/\/+$/, "");
const parts = path.split("/");
const windowId = parts[parts.length - 1] || "site-preview";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <DeskApp windowId={windowId} />
  </React.StrictMode>
);
