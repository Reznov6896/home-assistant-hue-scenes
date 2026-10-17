// hue-correctifs.js — deux correctifs de la carte HACS `hue-like-light-card`, posés À CÔTÉ d'elle.
//
// POURQUOI ICI : le dossier `www/community/` appartient à HACS. Quand HACS met à jour ou réinstalle
//    la carte, il remplace ses fichiers, et un correctif écrit dans son bundle disparaît sans bruit.
//    Ce module vit à la RACINE de `www/`, où HACS ne touche jamais : il corrige la carte au
//    chargement, sans modifier son fichier.
//
// ═══ CORRECTIF 1 — les lampes trop basses sur téléphone ═════════════════════════════════════
// La carte embarque :
//     @media all and (max-width: 450px), all and (max-height: 500px){
//         .detail-active .tile-scroller.light-tiles{ position:absolute; bottom:30px; ... }
//     }
// et la classe `detail-active` est posée EN PERMANENCE sur le conteneur, pas seulement quand on
// ouvre le détail d'une lampe. Sous 450 px, la rangée « Lumières » quitte donc le flux et se colle
// à 30 px du bas du dialogue. Or HA passe ses dialogues en PLEIN ÉCRAN à cette même largeur :
// « 30 px du bas » devient « 30 px du bas de l'ÉCRAN », d'où le grand vide entre le titre
// « Lumières » et les tuiles. Sur PC le dialogue est court, la règle ne s'applique pas.
// Mesure (dialogue ouvert, fenêtre de 987 px) :
//   écart scènes→lumières 17 px · règle forcée −167 px · correctif par-dessus 17 px (retour exact).
//
// ═══ CORRECTIF 2 — l'erreur à la fermeture du dialogue ══════════════════════════════════════
// `HueDialog.prototype.close()` fait :
//     const e = this.getDialogElement();  e ? e.close() : this.onDialogClose();
// `getDialogElement()` cherche un `<ha-dialog>` dans son rendu. Au retour arrière, l'élément trouvé
// n'est pas encore « upgradé » (un élément HTML nu) : `e` existe mais n'a pas de méthode `close`
// → `TypeError`.
// ON NE RÉÉCRIT PAS LA MÉTHODE, ON L'ENVELOPPE : on appelle l'originale, et on ne retombe sur
// `onDialogClose()` (le repli déjà prévu par l'auteur) que si elle LÈVE. Si une future version de
// la carte fait davantage dans `close()`, on le garde : on n'intervient que sur l'échec réel.
//
// PIÈGE : patcher `connectedCallback` APRÈS `customElements.define()` ne sert à RIEN — le navigateur
//    lit les callbacks de cycle de vie une seule fois, à l'enregistrement. `close()` et
//    `firstUpdated()` sont appelés par la carte et par Lit, eux : les patcher marche.
//
// CONTRÔLE : dans la console du navigateur, `window.__correctifHue` liste les accroches posées.
// RETOUR ARRIÈRE : retirer la ressource /local/hue-correctifs.js des ressources du tableau de bord.

const MARQUE = 'correctif-lumieres-mobile';

const CSS = `
@media all and (max-width: 450px), all and (max-height: 500px) {
  .detail-active .tile-scroller.light-tiles {
    position: static !important;
    bottom: auto !important;
    width: auto !important;
  }
}`;

window.__correctifHue = { charge: true, voie: [], poses: 0, fermetures_rattrapees: 0 };

function poser(el) {
  const sr = el && el.shadowRoot;
  if (!sr || sr.querySelector('#' + MARQUE)) return;
  const s = document.createElement('style');
  s.id = MARQUE;
  s.textContent = CSS;
  sr.appendChild(s);
  window.__correctifHue.poses++;
}

customElements.whenDefined('hue-dialog').then(() => {
  const C = customElements.get('hue-dialog');
  if (!C || !C.prototype) return;

  // ── CORRECTIF 1, voie a : la feuille Lit, adoptée par toute nouvelle instance
  try {
    if (Array.isArray(C.elementStyles)) {
      const f = new CSSStyleSheet();
      f.replaceSync(CSS);
      C.elementStyles = [...C.elementStyles, f];
      window.__correctifHue.voie.push('elementStyles');
    }
  } catch (e) { /* sans conséquence, la voie b prend le relais */ }

  // ── CORRECTIF 1, voie b : firstUpdated est appelé PAR Lit, donc patchable
  try {
    const origineMaj = C.prototype.firstUpdated;
    C.prototype.firstUpdated = function (modifs) {
      if (origineMaj) origineMaj.call(this, modifs);
      poser(this);
    };
    window.__correctifHue.voie.push('firstUpdated');
  } catch (e) { /* sans conséquence */ }

  // ── CORRECTIF 2 : close() enveloppé, jamais réécrit
  try {
    const origineClose = C.prototype.close;
    if (typeof origineClose === 'function') {
      C.prototype.close = function () {
        try {
          return origineClose.apply(this, arguments);
        } catch (err) {
          window.__correctifHue.fermetures_rattrapees++;
          try { this.onDialogClose(); } catch (e2) { /* rien de mieux à tenter */ }
        }
      };
      window.__correctifHue.voie.push('close');
    }
  } catch (e) { /* sans conséquence */ }

  // ── les dialogues déjà construits avant le chargement de ce module
  const balayer = (racine, prof) => {
    if (prof > 12 || !racine.querySelectorAll) return;
    racine.querySelectorAll('hue-dialog').forEach(poser);
    racine.querySelectorAll('*').forEach((n) => { if (n.shadowRoot) balayer(n.shadowRoot, prof + 1); });
  };
  try { balayer(document, 0); } catch (e) { /* sans conséquence */ }
});
