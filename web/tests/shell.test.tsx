import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach } from "vitest";
import { AppShell } from "../src/components/AppShell";
import { ThemeProvider } from "../src/theme/theme-provider";

function shell() {
  return render(
    <ThemeProvider defaultTheme="dark" storageKey="ors-theme">
      <AppShell>
        <p>rack</p>
      </AppShell>
    </ThemeProvider>,
  );
}

describe("the shell", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("light", "dark");
  });

  it("names every page the interface has", () => {
    shell();
    for (const page of ["Daemons", "Screens", "Templates", "Integrations", "Settings"]) {
      expect(screen.getByRole("link", { name: page })).toBeInTheDocument();
    }
  });

  it("starts dark, because that is what the design chose", () => {
    shell();
    expect(document.documentElement).toHaveClass("dark");
  });

  it("remembers a theme across a reload", async () => {
    shell();
    await userEvent.click(screen.getByRole("button", { name: /toggle theme/i }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Light" }));

    expect(document.documentElement).toHaveClass("light");
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("ors-theme")).toBe("light");
  });
});
