// hue-card-fixes.js — two fixes for the HACS card `hue-like-light-card`, loaded NEXT TO it.
//
// WHY HERE: `www/community/` belongs to HACS. When HACS updates or reinstalls the card, it
//    replaces its files, and a fix written into its bundle disappears silently.
//    This module lives at the ROOT of `www/`, which HACS never touches: it patches the card
//    at load time, without modifying its file.
//
// === FIX 1 — lights pushed to the bottom on phones =========================================
// The card ships:
//     @media all and (max-width: 450px), all and (max-height: 500px){
//         .detail-active .tile-scroller.light-tiles{ position:absolute; bottom:30px; ... }
//     }
// and the `detail-active` class is set PERMANENTLY on the container, not only when a light's
// detail is open. Below 450 px, the lights row therefore leaves the flow and sticks 30 px from
// the bottom of the dialog. HA opens its dialogs FULLSCREEN at that same width: "30 px from the
// bottom" becomes "30 px from the bottom of the SCREEN", hence the large gap between the
// "Lights" title and the tiles. On desktop the dialog is short and the rule does not apply.
// Measured (dialog open, 987 px window):
//   scenes->lights gap 17 px · rule forced -167 px · fix on top 17 px (exact restore).
//
// === FIX 2 — error when closing the dialog =================================================
// `HueDialog.prototype.close()` does:
//     const e = this.getDialogElement();  e ? e.close() : this.onDialogClose();
// `getDialogElement()` looks for an `<ha-dialog>` in its render. On browser back, the element
// found is not "upgraded" yet (a bare HTML element): `e` exists but has no `close` method
// -> `TypeError`.
// THE METHOD IS WRAPPED, NOT REWRITTEN: the original is called, and `onDialogClose()` (the
// author's own fallback) is used only if it THROWS. If a future version of the card does more
// in `close()`, that is kept: the wrapper only acts on an actual failure.
//
// TRAP: patching `connectedCallback` AFTER `customElements.define()` does NOTHING — the browser
//    reads lifecycle callbacks once, at registration. `close()` and `firstUpdated()` are
//    called by the card and by Lit: patching those works.
//
// CHECK: in the browser console, `window.__hueCardFixes` lists the hooks in place.
// ROLLBACK: remove the /local/hue-card-fixes.js dashboard resource.

const MARKER = 'hue-card-fix-mobile-lights';

const CSS = `
@media all and (max-width: 450px), all and (max-height: 500px) {
  .detail-active .tile-scroller.light-tiles {
    position: static !important;
    bottom: auto !important;
    width: auto !important;
  }
}`;

window.__hueCardFixes = { loaded: true, hooks: [], applied: 0, closes_recovered: 0 };

function apply(el) {
  const sr = el && el.shadowRoot;
  if (!sr || sr.querySelector('#' + MARKER)) return;
  const s = document.createElement('style');
  s.id = MARKER;
  s.textContent = CSS;
  sr.appendChild(s);
  window.__hueCardFixes.applied++;
}

customElements.whenDefined('hue-dialog').then(() => {
  const C = customElements.get('hue-dialog');
  if (!C || !C.prototype) return;

  // -- FIX 1, path a: the Lit stylesheet, adopted by every new instance
  try {
    if (Array.isArray(C.elementStyles)) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
      C.elementStyles = [...C.elementStyles, sheet];
      window.__hueCardFixes.hooks.push('elementStyles');
    }
  } catch (e) { /* harmless, path b takes over */ }

  // -- FIX 1, path b: firstUpdated is called BY Lit, so it can be patched
  try {
    const originalFirstUpdated = C.prototype.firstUpdated;
    C.prototype.firstUpdated = function (changed) {
      if (originalFirstUpdated) originalFirstUpdated.call(this, changed);
      apply(this);
    };
    window.__hueCardFixes.hooks.push('firstUpdated');
  } catch (e) { /* harmless */ }

  // -- FIX 2: close() wrapped, never rewritten
  try {
    const originalClose = C.prototype.close;
    if (typeof originalClose === 'function') {
      C.prototype.close = function () {
        try {
          return originalClose.apply(this, arguments);
        } catch (err) {
          window.__hueCardFixes.closes_recovered++;
          try { this.onDialogClose(); } catch (e2) { /* nothing better to try */ }
        }
      };
      window.__hueCardFixes.hooks.push('close');
    }
  } catch (e) { /* harmless */ }

  // -- dialogs already built before this module loaded
  const sweep = (root, depth) => {
    if (depth > 12 || !root.querySelectorAll) return;
    root.querySelectorAll('hue-dialog').forEach(apply);
    root.querySelectorAll('*').forEach((n) => { if (n.shadowRoot) sweep(n.shadowRoot, depth + 1); });
  };
  try { sweep(document, 0); } catch (e) { /* harmless */ }
});
