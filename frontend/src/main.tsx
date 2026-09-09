import React from "react";
import ReactDOM from "react-dom/client";
import { AuthoringApp } from "./AuthoringApp";
import { queryClient } from "./app/query-client";
import { ThemeProvider } from "./app/theme";
import { QueryClientProvider } from "@tanstack/react-query";
import "./styles/tokens.css";
import "./styles/tailwind.css";
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthoringApp />
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>
);

