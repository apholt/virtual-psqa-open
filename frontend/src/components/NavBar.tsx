import { Topbar } from "./Topbar";

/**
 * Legacy wrapper kept for pages that imported NavBar. Renders the shared
 * light-theme Topbar (app title + nav + live clock).
 */
export function NavBar() {
  return <Topbar />;
}
