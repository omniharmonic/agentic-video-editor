// The Director's Room app: two surfaces and nothing else at the top level.
//   #/room/<artifact>   the Room (dark): the stage that follows the conversation with Claude
//   #/workspace...      the Workspace (light): every artifact across the projects on this computer
//   #/settings/...      Settings (light), reached from the Workspace's sidebar and the Room's header: accounts, alerts, people
//                       and collaboration. It is the back office, not a third surface.
// A bare /app/ opens the Room, as the desktop app always has.

import { mountRoom } from "./room/room.js";
import { mountWorkspace, parseRoute } from "./workspace/workspace.js";
import { mountSettings, parseSettings } from "./settings.js";

function route() {
  const h = location.hash;
  if (h.startsWith("#/workspace")) return { surface: "workspace", ws: parseRoute(h) };
  if (h.startsWith("#/settings")) return { surface: "settings", set: parseSettings(h) };
  const m = h.match(/^#\/room\/(.+)$/);
  return { surface: "room", artifact: m ? decodeURIComponent(m[1]) : null };
}

let mounted = null;
function go() {
  const r = route();
  const app = document.getElementById("app");
  if (r.surface === "workspace") {
    document.documentElement.dataset.theme = "light";
    if (mounted?.surface === "workspace") { mounted.view.setRoute(r.ws); return; }
    mounted?.view.close();
    mounted = { surface: "workspace", view: mountWorkspace(app, r.ws) };
    return;
  }
  if (r.surface === "settings") {
    document.documentElement.dataset.theme = "light";
    if (mounted?.surface === "settings") { mounted.view.setRoute(r.set); return; }
    mounted?.view.close();
    mounted = { surface: "settings", view: mountSettings(app, r.set) };
    return;
  }
  document.documentElement.dataset.theme = "dark";
  if (mounted?.surface === "room" && mounted.artifact === r.artifact) return;
  mounted?.view.close();
  mounted = { surface: "room", artifact: r.artifact, view: mountRoom(app, { artifact: r.artifact }) };
}

window.addEventListener("hashchange", go);
go();
