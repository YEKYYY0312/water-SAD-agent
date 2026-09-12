import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { Workbench } from "./Workbench";
import "./styles.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Missing #root application element");
}

createRoot(root).render(
  <StrictMode>
    <Workbench />
  </StrictMode>,
);
