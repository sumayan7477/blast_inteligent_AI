import { useEffect, useState } from "react";

export type Page = "dashboard" | "analytics" | "assistant";

export interface Route {
  page: Page;
  tab: string | null;
}

export function parseHash(hash: string): Route {
  const [, page, tab] = hash.replace(/^#/, "").split("/");
  if (page === "analytics" || page === "assistant" || page === "dashboard") {
    return { page, tab: tab || null };
  }
  return { page: "dashboard", tab: null };
}

export function navigate(page: Page, tab?: string): void {
  window.location.hash = tab ? `/${page}/${tab}` : `/${page}`;
}

/** Minimal hash router (no dependency); only the page name is in the URL - never filters or data. */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
