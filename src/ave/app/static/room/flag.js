// The flag popover every view shares: a one-line note for Claude, sent with the director's next message.
// `send(note)` posts the flag; the popover closes when it lands and stays open (with the note) when it fails.

import { h, button, busy, popover } from "../dom.js";

export function flagPopover(anchor, { title, hint = "Claude reads it with your next message.", send, align = "end", place = "below" }) {
  const note = h("input", { class: "input", id: "flag-note", maxlength: "500", "aria-label": "Note for Claude", placeholder: "Too long here, try the wide shot" });
  const ok = button("Flag for Claude", { variant: "primary", size: "sm", "data-edit": "1" });
  const cancel = button("Cancel", { variant: "tertiary", size: "sm" });
  const form = h("form", { class: "flag-form" },
    h("p", { class: "flag-head" }, h("span", { class: "popover-title" }, title), h("span", { class: "flag-hint secondary-text" }, hint)),
    h("div", { class: "flag-row" }, note, cancel, ok));
  const pop = popover(anchor, form, { place, align, modal: true, wide: true, label: "Flag for Claude" });
  cancel.addEventListener("click", () => pop.close());
  form.addEventListener("submit", (e) => e.preventDefault());
  note.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ok.click(); } });
  ok.addEventListener("click", async () => {
    try {
      await busy(ok, () => send(note.value.trim()));
      pop.close();
    } catch { /* the caller said what went wrong; the note stays so nothing is lost */ }
  });
  note.focus();
  return pop;
}
