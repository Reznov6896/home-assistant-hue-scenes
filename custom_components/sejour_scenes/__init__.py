"""Scènes du séjour : la liste éditable des scènes de la carte `hue-like-light-card`.

Composant maison, non suivi par HACS : aucune mise à jour ne l'écrase.
Il tient la liste (fichier `sejour_scenes.json`), la publie dans l'attribut `list` de
`sensor.sejour_scenes_list` au format attendu par la carte (injecté par auto-entities),
et offre cinq services : add, remove, rename, move, capture.
"""
import json
import logging
import os
import re
import time
import unicodedata

import voluptuous as vol
import yaml

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import color as color_util

GERER_SELECT_ENTITY = "input_select.sejour_gerer_choix"
GROUPE_ENTITY = "light.sejour_toutes_les_lampes"  # le groupe : jamais enregistré dans une capture

DOMAIN = "sejour_scenes"
_LOGGER = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "sejour_scenes.json")
SCENES_YAML_PATH = "/config/scenes.yaml"
SENSOR_ENTITY = "sensor.sejour_scenes_list"
ICONE_DEFAUT = "mdi:palette"

# `icon` sans valeur par défaut dans les schémas : None veut dire « choisir d'après les lampes »
# (tuile unie si une seule couleur, mdi:palette sinon).
ADD_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("title"): cv.string,
    vol.Optional("icon"): cv.string,
    vol.Optional("color"): cv.string,
    vol.Optional("position"): vol.Coerce(int),
})
REMOVE_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id})
RENAME_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("title"): cv.string,
})
MOVE_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("position"): vol.Coerce(int),
})
CAPTURE_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Optional("icon"): cv.string,
    vol.Optional("color"): cv.string,
})


# ==== COULEUR DE LA PASTILLE ====
# Une scène ajoutée ou capturée sans `color` recevait l'icône orange par défaut de la carte.
# La couleur est donc calculée d'après l'état des lampes : la teinte DOMINANTE des lampes allumées
# (celle qui pèse le plus, nombre de lampes × luminosité), rendue à pleine saturation comme HA
# affiche la couleur d'une lampe. Une seule teinte -> exactement celle-là.
# Écarté : le mélange additif, essayé, délave les scènes multicolores (presque blanc). La dominante
# est aussi la règle des pastilles faites à la main. La luminosité ne change pas la teinte
# (« Rouge » et « Rouge 50 % » ont la même pastille).
def _xy_de(etat: dict):
    if etat.get("xy_color"):
        x, y = etat["xy_color"]
        return float(x), float(y)
    if etat.get("hs_color"):
        return color_util.color_hs_to_xy(*[float(v) for v in etat["hs_color"]])
    if etat.get("rgb_color"):
        return color_util.color_RGB_to_xy(*[int(v) for v in etat["rgb_color"][:3]])
    kelvin = etat.get("color_temp_kelvin")
    if not kelvin and etat.get("color_temp"):
        kelvin = 1_000_000 / float(etat["color_temp"])
    if kelvin:
        r, g, b = color_util.color_temperature_to_rgb(float(kelvin))
        return color_util.color_RGB_to_xy(int(r), int(g), int(b))
    return None


def _couleur_pastille(entites: dict):
    """Rend (couleur '#rrggbb', unie) — unie=True si toutes les lampes allumées ont la même
    teinte — ou (None, None) si aucune lampe allumée n'a de couleur."""
    groupes = {}  # teinte arrondie -> [poids, somme x, somme y]
    for etat in (entites or {}).values():
        if not isinstance(etat, dict) or etat.get("state") not in ("on", True):
            continue
        xy = _xy_de(etat)
        if not xy or xy[1] <= 0:
            continue
        x, y = xy
        lum = float(etat.get("brightness") or 255) / 255.0
        g = groupes.setdefault((round(x, 2), round(y, 2)), [0.0, 0.0, 0.0])
        g[0] += lum
        g[1] += x * lum
        g[2] += y * lum
    if not groupes:
        return None, None
    poids, sx, sy = max(groupes.values(), key=lambda g: g[0])
    if poids <= 0:
        return None, None
    h, s = color_util.color_xy_to_hs(sx / poids, sy / poids)
    r, g, b = color_util.color_hs_to_RGB(h, s)
    return "#%02x%02x%02x" % (r, g, b), len(groupes) == 1


# ==== TUILE UNIE POUR UNE SCÈNE D'UNE SEULE COULEUR ====
# Piège : `icon: mdi:circle` + `color` dessine un ROND BLANC au milieu de la pastille (la carte
# peint l'icône en blanc dès que la scène n'a pas d'image). Parade : une image 400×400 d'une seule
# couleur, rattachée à la scène par `entity_picture` ; l'image remplace alors l'icône.
# L'image va dans www/scene_tiles/, son rattachement dans packages/sejour_pastilles.yaml, fichier
# GÉNÉRÉ que HA fusionne avec le `customize:` de configuration.yaml (jamais touché, lui).
# Scène multicolore -> icône palette + couleur dominante : pas de rond blanc.
TUILES_DIR = "/config/www/scene_tiles"
PAQUET_PATH = "/config/packages/sejour_pastilles.yaml"


def _ecrire_tuile(slug: str, couleur: str, deja_a_nous: set) -> str:
    from PIL import Image
    os.makedirs(TUILES_DIR, exist_ok=True)
    nom = f"solid_{slug}.jpeg"
    if os.path.exists(os.path.join(TUILES_DIR, nom)) and nom not in deja_a_nous:
        nom = f"solid_{slug}_auto.jpeg"  # ne jamais écraser une tuile faite à la main
    rgb = tuple(int(couleur[i:i + 2], 16) for i in (1, 3, 5))
    Image.new("RGB", (400, 400), rgb).save(os.path.join(TUILES_DIR, nom), "JPEG", quality=95)
    return nom


def _ecrire_paquet(tuiles: dict):
    os.makedirs(os.path.dirname(PAQUET_PATH), exist_ok=True)
    doc = {"homeassistant": {"customize": {
        e: {"entity_picture": t["url"]} for e, t in sorted(tuiles.items())}}}
    texte = ("# GÉNÉRÉ par custom_components/sejour_scenes (tuiles unies des pastilles).\n"
             "# Ne pas éditer à la main : réécrit à chaque capture ou ajout.\n"
             + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False))
    tmp = PAQUET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texte)
    os.replace(tmp, PAQUET_PATH)


def _entites_de_la_scene(scene_id: str):
    """Les états enregistrés d'une scène de scenes.yaml, retrouvée par son `id:`."""
    with open(SCENES_YAML_PATH, "r", encoding="utf-8") as f:
        scenes = yaml.safe_load(f) or []
    for sc in scenes:
        if isinstance(sc, dict) and sc.get("id") == scene_id:
            return sc.get("entities") or {}
    return None


def _load():
    # Premier démarrage : pas encore de fichier, la liste part vide.
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-zA-Z0-9]+", "_", n).strip("_").lower()
    return n or "scene"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    data = await hass.async_add_executor_job(_load)
    data.setdefault("scenes", [])
    data.setdefault("lights", [])
    data.setdefault("tuiles", {})  # entity_id -> {"fichier", "url", "couleur"} (tuiles générées ici)

    async def _entites_depuis_scene(entity_id: str):
        # L'`id:` d'une scène YAML est son unique_id au registre (ex. scene_rouge).
        ent = er.async_get(hass).async_get(entity_id)
        scene_id = ent.unique_id if ent else "scene_" + entity_id.split(".", 1)[1]
        return await hass.async_add_executor_job(_entites_de_la_scene, scene_id)

    async def _habiller(entry: dict, entites=None) -> bool:
        """Donne sa couleur à une pastille, et une tuile unie si elle est d'une seule
        couleur et n'a pas déjà d'image. Rend True si `entry` a changé."""
        eid = entry["entity"]
        if entites is None:
            entites = await _entites_depuis_scene(eid)
        couleur, unie = _couleur_pastille(entites)
        if not couleur:
            return False
        change = False
        if not entry.get("color"):
            entry["color"] = couleur
            change = True
        st = hass.states.get(eid)
        image = st.attributes.get("entity_picture") if st else None
        a_nous = data["tuiles"].get(eid)
        if unie and (not image or a_nous):
            fichiers = {t["fichier"] for t in data["tuiles"].values()}
            nom = await hass.async_add_executor_job(_ecrire_tuile, eid.split(".", 1)[1], entry["color"], fichiers)
            data["tuiles"][eid] = {"fichier": nom, "couleur": entry["color"],
                                   "url": f"/local/scene_tiles/{nom}?v={int(time.time())}"}
            await hass.async_add_executor_job(_ecrire_paquet, data["tuiles"])
            await hass.services.async_call("homeassistant", "reload_core_config", blocking=True)
            await hass.services.async_call("homeassistant", "update_entity", {"entity_id": [eid]}, blocking=True)
            _LOGGER.info("sejour_scenes: tuile unie %s (%s) rattachée à %s", nom, entry["color"], eid)
            change = True
        elif not unie and entry.get("icon") in (None, "mdi:circle"):
            entry["icon"] = ICONE_DEFAUT
            change = True
        if not entry.get("icon"):
            entry["icon"] = ICONE_DEFAUT
            change = True
        return change

    async def _rattrapage(_event=None):
        # Au démarrage (états chargés) : pastille sans couleur, ou « mdi:circle » sans image
        # (= rond blanc). Les pastilles déjà propres ne sont jamais touchées.
        faites = []
        for s in data["scenes"]:
            st = hass.states.get(s["entity"])
            sans_image = not (st and st.attributes.get("entity_picture"))
            if s.get("color") and not (s.get("icon") == "mdi:circle" and sans_image):
                continue
            try:
                if await _habiller(s):
                    faites.append(f"{s['entity']}={s.get('color')}")
            except Exception as err:  # une scène illisible ne doit pas bloquer la liste
                _LOGGER.warning("sejour_scenes: pastille de %s non traitée : %s", s["entity"], err)
        if faites:
            await hass.async_add_executor_job(_save, data)
            _publish()
            _LOGGER.info("sejour_scenes: pastilles rattrapées : %s", ", ".join(faites))

    def _publish():
        selected_state = hass.states.get(GERER_SELECT_ENTITY)
        selected_option = selected_state.state if selected_state else None

        rows = []
        for i, s in enumerate(data["scenes"]):
            select_value = f"{s['title']} || {s['entity']}"
            is_selected = selected_option == select_value
            # Ligne maison `scene-choix-row` (www/scene-choix-row.js) : même rendu que la ligne de
            # scène native (image + nom + « Activer »), mais le bouton SÉLECTIONNE la scène dans
            # « Scénario concerné » au lieu de l'allumer.
            # Écarté : `type: button`, la ligne perd l'image de la scène.
            rows.append({
                "type": "custom:scene-choix-row",
                "action_name": "Activer",
                "entity": s["entity"],
                "name": f"✅ {s['title']}" if is_selected else s["title"],
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "input_select.select_option",
                    "target": {"entity_id": GERER_SELECT_ENTITY},
                    "data": {"option": select_value},
                },
            })
            if i < len(data["scenes"]) - 1:
                rows.append({"type": "divider"})

        hass.states.async_set(
            SENSOR_ENTITY,
            "ok",
            {
                "list": data["scenes"],
                "rows": rows,
                "friendly_name": "Séjour - liste des scénarios",
                "icon": "mdi:palette-swatch",
            },
            # force_update=True est indispensable : `rows` et `list` sont reconstruits à partir de
            # data["scenes"], que les services modifient en place ; l'ancien et le nouvel état peuvent
            # donc contenir les mêmes objets. Sans force_update, HA voit des attributs égaux et ignore
            # la mise à jour, sans erreur ni journal : l'interface ne change jamais. Constaté après
            # trois redémarrages de débogage. Ne pas retirer.
            force_update=True,
        )

    _publish()

    if hass.state is CoreState.running:
        hass.async_create_task(_rattrapage())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _rattrapage)

    async def _on_selection_changed(event):
        _publish()

    async_track_state_change_event(hass, [GERER_SELECT_ENTITY], _on_selection_changed)

    async def handle_add(call: ServiceCall):
        entity_id = call.data["entity_id"]
        title = call.data.get("title")
        icon = call.data.get("icon")
        color = call.data.get("color")
        position = call.data.get("position")

        if not entity_id.startswith("scene."):
            raise ValueError(f"{entity_id} n'est pas une entité scene.*")
        state = hass.states.get(entity_id)
        if state is None:
            raise ValueError(f"{entity_id} n'existe pas")
        if not title:
            title = state.attributes.get("friendly_name", entity_id)

        data["scenes"] = [s for s in data["scenes"] if s["entity"] != entity_id]
        entry = {"entity": entity_id, "title": title}
        if icon:
            entry["icon"] = icon
        if color:
            entry["color"] = color
        try:
            await _habiller(entry)
        except Exception as err:  # la pastille reste ajoutée, simplement sans couleur calculée
            _LOGGER.warning("sejour_scenes: pastille de %s non habillée : %s", entity_id, err)
        entry.setdefault("icon", ICONE_DEFAUT)
        if position is None or position >= len(data["scenes"]):
            data["scenes"].append(entry)
        else:
            data["scenes"].insert(max(0, position), entry)

        await hass.async_add_executor_job(_save, data)
        _publish()

    async def handle_remove(call: ServiceCall):
        entity_id = call.data["entity_id"]
        data["scenes"] = [s for s in data["scenes"] if s["entity"] != entity_id]
        await hass.async_add_executor_job(_save, data)
        _publish()

    async def handle_rename(call: ServiceCall):
        entity_id = call.data["entity_id"]
        title = call.data["title"]
        for s in data["scenes"]:
            if s["entity"] == entity_id:
                s["title"] = title
        await hass.async_add_executor_job(_save, data)
        _publish()

    async def handle_move(call: ServiceCall):
        entity_id = call.data["entity_id"]
        position = call.data["position"]
        idx = next((i for i, s in enumerate(data["scenes"]) if s["entity"] == entity_id), None)
        if idx is None:
            return
        entry = data["scenes"].pop(idx)
        position = max(0, min(position, len(data["scenes"])))
        data["scenes"].insert(position, entry)
        await hass.async_add_executor_job(_save, data)
        _publish()

    async def handle_capture(call: ServiceCall):
        name = call.data["name"]
        icon = call.data.get("icon")
        color = call.data.get("color")

        # L'état exact de chaque lampe (allumée ou non, couleur xy, luminosité), sans le groupe.
        lights = [e for e in data["lights"] if e != GROUPE_ENTITY]
        entities_block = {}
        for light in lights:
            st = hass.states.get(light)
            if st is None:
                continue
            attrs = st.attributes
            entry = {"state": st.state}
            if "xy_color" in attrs:
                entry["xy_color"] = [round(float(v), 4) for v in attrs["xy_color"]]
            if "brightness" in attrs:
                entry["brightness"] = attrs["brightness"]
            entities_block[light] = entry

        base_slug = _slugify(name)
        result = {}

        def _write_scene():
            # Ajout EN FIN de scenes.yaml : le reste du fichier (et ses commentaires) n'est pas réécrit.
            with open(SCENES_YAML_PATH, "r", encoding="utf-8") as f:
                existing = f.read()
            candidate = base_slug
            i = 1
            while f"- id: scene_{candidate}\n" in existing:
                i += 1
                candidate = f"{base_slug}_{i}"
            result["slug"] = candidate
            block = [{"id": f"scene_{candidate}", "name": name, "entities": entities_block}]
            text = yaml.safe_dump(block, allow_unicode=True, sort_keys=False, default_flow_style=False)
            with open(SCENES_YAML_PATH, "a", encoding="utf-8") as f:
                f.write("\n" + text)

        await hass.async_add_executor_job(_write_scene)
        await hass.services.async_call("scene", "reload", blocking=True)

        new_entity_id = f"scene.{result['slug']}"
        entry = {"entity": new_entity_id, "title": name}
        if icon:
            entry["icon"] = icon
        if color:
            entry["color"] = color
        try:
            await _habiller(entry, entities_block)
        except Exception as err:  # la scène est capturée quoi qu'il arrive
            _LOGGER.warning("sejour_scenes: pastille de %s non habillée : %s", new_entity_id, err)
        entry.setdefault("icon", ICONE_DEFAUT)
        data["scenes"].append(entry)
        await hass.async_add_executor_job(_save, data)
        _publish()
        _LOGGER.info("sejour_scenes: état actuel des lampes capturé sous %s", new_entity_id)

    hass.services.async_register(DOMAIN, "add", handle_add, schema=ADD_SCHEMA)
    hass.services.async_register(DOMAIN, "remove", handle_remove, schema=REMOVE_SCHEMA)
    hass.services.async_register(DOMAIN, "rename", handle_rename, schema=RENAME_SCHEMA)
    hass.services.async_register(DOMAIN, "move", handle_move, schema=MOVE_SCHEMA)
    hass.services.async_register(DOMAIN, "capture", handle_capture, schema=CAPTURE_SCHEMA)

    return True
