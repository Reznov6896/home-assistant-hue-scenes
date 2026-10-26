// scene-select-row.js — custom entity row for the "Move / rename" panel.
// Each row's button SELECTS the scene in the manage dropdown instead of activating it:
// - HA's native scene row always activates (its button ignores `tap_action`);
// - HA's `button` row runs the wanted action but only shows an icon, not the scene picture.
// This row therefore rebuilds HA 2026.7's native scene row — `hui-generic-entity-row`
// (picture or icon + name) + an `ha-button` (appearance plain, size s) — and only the button
// click changes: it runs the row's `tap_action` (provided by custom_components/room_scenes).
// Clicking the name keeps the same `tap_action`.
// Limitation: `hui-generic-entity-row` and `ha-button` are HA internals, not a public API;
// a future version may change them.
// Resource: /local/scene-select-row.js (outside www/community/, so safe from HACS updates).
class SceneSelectRow extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error('entity required');
    this._config = config;
    if (this._row) this._row.config = this._rowConfig();
  }
  _rowConfig() {
    const c = { entity: this._config.entity, name: this._config.name };
    if (this._config.tap_action) c.tap_action = this._config.tap_action;
    return c;
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._row) {
      this._row = document.createElement('hui-generic-entity-row');
      this._button = document.createElement('ha-button');
      this._button.setAttribute('appearance', 'plain');
      this._button.setAttribute('size', 's');
      this._button.classList.add('text-content');
      this._button.addEventListener('click', (ev) => { ev.stopPropagation(); this._act(); });
      this._row.appendChild(this._button);
      this.appendChild(this._row);
    }
    this._row.hass = hass;
    this._row.config = this._rowConfig();
    this._button.textContent = this._config.action_name || hass.localize('ui.card.scene.activate') || 'Activate';
  }
  _act() {
    const a = this._config.tap_action || {};
    const action = a.perform_action || a.service;
    if (!action || !this._hass) return;
    const [domain, service] = action.split('.');
    this._hass.callService(domain, service, a.data || {}, a.target || undefined);
  }
  getCardSize() { return 1; }
}
if (!customElements.get('scene-select-row')) customElements.define('scene-select-row', SceneSelectRow);
