import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { ThemeProvider, useTheme } from "./theme";

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useTheme();
  return (
    <button onClick={() => setPreference("dark")}>{preference}:{resolvedTheme}</button>
  );
}

describe("ThemeProvider", () => {
  beforeEach(() => window.localStorage.clear());

  it("persists an explicit theme preference", async () => {
    const user = userEvent.setup();
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>);
    await user.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveTextContent("dark:dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("novelpilot.theme")).toBe("dark");
  });
});
