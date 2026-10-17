// scene-choix-row.js — une ligne de liste maison, pour le panneau « Déplacer / renommer ».
// Le bouton « Activer » de chaque ligne SÉLECTIONNE la scène dans « Scénario concerné » au lieu de
// l'allumer :
// - la ligne de scène native de HA allume en dur (son bouton ignore `tap_action`) ;
// - la ligne `button` de HA fait l'action voulue, mais n'affiche qu'une icône, sans l'image de la scène.
// Cette ligne reprend donc la ligne de scène native de HA 2026.7 — `hui-generic-entity-row`
// (image ou icône + nom) + un `ha-button` « Activer » (appearance plain, size s) — et seul le clic du
// bouton change : il exécute le `tap_action` de la ligne (fourni par custom_components/sejour_scenes).
// Le clic sur le nom garde le même `tap_action`.
// Limite : `hui-generic-entity-row` et `ha-button` sont des éléments internes de HA, pas une API
// publique ; une version future peut les changer.
// Ressource : /local/scene-choix-row.js (hors de www/community/, donc à l'abri des mises à jour HACS).
class SceneChoixRow extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error('entity requis');
    this._config = config;
    if (this._ligne) this._ligne.config = this._configLigne();
  }
  _configLigne() {
    const c = { entity: this._config.entity, name: this._config.name };
    if (this._config.tap_action) c.tap_action = this._config.tap_action;
    return c;
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._ligne) {
      this._ligne = document.createElement('hui-generic-entity-row');
      this._bouton = document.createElement('ha-button');
      this._bouton.setAttribute('appearance', 'plain');
      this._bouton.setAttribute('size', 's');
      this._bouton.classList.add('text-content');
      this._bouton.addEventListener('click', (ev) => { ev.stopPropagation(); this._agir(); });
      this._ligne.appendChild(this._bouton);
      this.appendChild(this._ligne);
    }
    this._ligne.hass = hass;
    this._ligne.config = this._configLigne();
    this._bouton.textContent = this._config.action_name || hass.localize('ui.card.scene.activate') || 'Activer';
  }
  _agir() {
    const a = this._config.tap_action || {};
    const action = a.perform_action || a.service;
    if (!action || !this._hass) return;
    const [domaine, service] = action.split('.');
    this._hass.callService(domaine, service, a.data || {}, a.target || undefined);
  }
  getCardSize() { return 1; }
}
if (!customElements.get('scene-choix-row')) customElements.define('scene-choix-row', SceneChoixRow);
