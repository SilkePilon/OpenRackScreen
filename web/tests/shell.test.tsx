import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach } from "vitest";
import { MemoryRouter } from "react-router";
import { AppShell } from "../src/components/AppShell";
import { ThemeProvider } from "../src/theme/theme-provider";

// The sidebar's nav items are router links now, and a `Link` outside a router
// throws. This is the shell on its own, so a memory history is enough -- the
// whole interface at a route is `renderApp` in ./render.
//
// The QueryClient arrived with the rack-status strip: the shell reads the
// racks it draws dots for out of the cache, and owns the `/ws/ui` connection
// that keeps them true. Nothing here fetches -- the strip has no query function
// and the socket is `setup.ts`'s inert one -- so this client stays empty and
// the strip stays rightly blank. What it draws when it is *not* empty is
// `panel.test.tsx`'s.
function shell() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ThemeProvider defaultTheme="dark" storageKey="ors-theme">
        <MemoryRouter>
          <AppShell>
            <p>rack</p>
          </AppShell>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>,
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

  it("restores the stored theme over the default", () => {
    localStorage.setItem("ors-theme", "light");
    shell();
    expect(document.documentElement).toHaveClass("light");
    expect(document.documentElement).not.toHaveClass("dark");
  });
});
