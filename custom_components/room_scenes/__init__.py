"""Room scenes: the editable scene list of the `hue-like-light-card`.

Standalone component, not tracked by HACS: no update overwrites it.
It stores the list (`room_scenes.json`), publishes it in the `list` attribute of
`sensor.room_scenes_list` in the format the card expects (injected by auto-entities),
and provides five services: add, remove, rename, move, capture.
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

MANAGE_SELECT_ENTITY = "input_select.room_scenes_manage"
GROUP_ENTITY = "light.living_room_all"  # the group: never stored in a capture

DOMAIN = "room_scenes"
_LOGGER = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "room_scenes.json")
SCENES_YAML_PATH = "/config/scenes.yaml"
SENSOR_ENTITY = "sensor.room_scenes_list"
DEFAULT_ICON = "mdi:palette"

# `icon` has no default in the schemas: None means "derive from the lights"
# (solid tile for a single colour, mdi:palette otherwise).
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


# ==== SWATCH COLOUR ====
# A scene added or captured without `color` got the card's default orange icon.
# The colour is therefore derived from the lights: the DOMINANT hue of the lights that are on
# (the one with the most weight, number of lights x brightness), at full saturation, the way HA
# shows a light's colour. A single hue -> exactly that hue.
# Rejected: additive mixing, which washes multicolour scenes out (almost white). Brightness does
# not change the hue ("Red" and "Red 50 %" get the same swatch).
def _xy_of(state: dict):
    if state.get("xy_color"):
        x, y = state["xy_color"]
        return float(x), float(y)
    if state.get("hs_color"):
        return color_util.color_hs_to_xy(*[float(v) for v in state["hs_color"]])
    if state.get("rgb_color"):
        return color_util.color_RGB_to_xy(*[int(v) for v in state["rgb_color"][:3]])
    kelvin = state.get("color_temp_kelvin")
    if not kelvin and state.get("color_temp"):
        kelvin = 1_000_000 / float(state["color_temp"])
    if kelvin:
        r, g, b = color_util.color_temperature_to_rgb(float(kelvin))
        return color_util.color_RGB_to_xy(int(r), int(g), int(b))
    return None


def _swatch_color(entities: dict):
    """Return (colour '#rrggbb', solid) - solid=True if every light that is on has the same
    hue - or (None, None) if no light that is on has a colour."""
    groups = {}  # rounded hue -> [weight, sum x, sum y]
    for state in (entities or {}).values():
        if not isinstance(state, dict) or state.get("state") not in ("on", True):
            continue
        xy = _xy_of(state)
        if not xy or xy[1] <= 0:
            continue
        x, y = xy
        bri = float(state.get("brightness") or 255) / 255.0
        g = groups.setdefault((round(x, 2), round(y, 2)), [0.0, 0.0, 0.0])
        g[0] += bri
        g[1] += x * bri
        g[2] += y * bri
    if not groups:
        return None, None
    weight, sx, sy = max(groups.values(), key=lambda g: g[0])
    if weight <= 0:
        return None, None
    h, s = color_util.color_xy_to_hs(sx / weight, sy / weight)
    r, g, b = color_util.color_hs_to_RGB(h, s)
    return "#%02x%02x%02x" % (r, g, b), len(groups) == 1


# ==== SOLID TILE FOR A SINGLE-COLOUR SCENE ====
# Trap: `icon: mdi:circle` + `color` draws a WHITE DOT in the middle of the swatch (the card paints
# the icon white whenever the scene has no picture). Workaround: a 400x400 single-colour image,
# attached to the scene through `entity_picture`; the picture then replaces the icon.
# The image goes to www/scene_tiles/, its attachment to packages/room_scenes_tiles.yaml, a
# GENERATED file that HA merges with the `customize:` of configuration.yaml (never touched).
# Multicolour scene -> palette icon + dominant colour: no white dot.
TILES_DIR = "/config/www/scene_tiles"
PACKAGE_PATH = "/config/packages/room_scenes_tiles.yaml"


def _write_tile(slug: str, color: str, ours: set) -> str:
    from PIL import Image
    os.makedirs(TILES_DIR, exist_ok=True)
    name = f"solid_{slug}.jpeg"
    if os.path.exists(os.path.join(TILES_DIR, name)) and name not in ours:
        name = f"solid_{slug}_auto.jpeg"  # never overwrite a hand-made tile
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    Image.new("RGB", (400, 400), rgb).save(os.path.join(TILES_DIR, name), "JPEG", quality=95)
    return name


def _write_package(tiles: dict):
    os.makedirs(os.path.dirname(PACKAGE_PATH), exist_ok=True)
    doc = {"homeassistant": {"customize": {
        e: {"entity_picture": t["url"]} for e, t in sorted(tiles.items())}}}
    text = ("# GENERATED by custom_components/room_scenes (solid swatch tiles).\n"
            "# Do not edit by hand: rewritten on every capture or add.\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False))
    tmp = PACKAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, PACKAGE_PATH)


def _scene_entities(scene_id: str):
    """The stored states of a scenes.yaml scene, found by its `id:`."""
    with open(SCENES_YAML_PATH, "r", encoding="utf-8") as f:
        scenes = yaml.safe_load(f) or []
    for sc in scenes:
        if isinstance(sc, dict) and sc.get("id") == scene_id:
            return sc.get("entities") or {}
    return None


def _load():
    # First start: no file yet, the list starts empty.
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
    data.setdefault("tiles", {})  # entity_id -> {"file", "url", "color"} (tiles generated here)

    async def _entities_from_scene(entity_id: str):
        # The `id:` of a YAML scene is its unique_id in the registry (e.g. scene_red).
        ent = er.async_get(hass).async_get(entity_id)
        scene_id = ent.unique_id if ent else "scene_" + entity_id.split(".", 1)[1]
        return await hass.async_add_executor_job(_scene_entities, scene_id)

    async def _dress(entry: dict, entities=None) -> bool:
        """Give a swatch its colour, and a solid tile if it is single-colour and has no
        picture yet. Return True if `entry` changed."""
        eid = entry["entity"]
        if entities is None:
            entities = await _entities_from_scene(eid)
        color, solid = _swatch_color(entities)
        if not color:
            return False
        changed = False
        if not entry.get("color"):
            entry["color"] = color
            changed = True
        st = hass.states.get(eid)
        picture = st.attributes.get("entity_picture") if st else None
        ours = data["tiles"].get(eid)
        if solid and (not picture or ours):
            files = {t["file"] for t in data["tiles"].values()}
            name = await hass.async_add_executor_job(_write_tile, eid.split(".", 1)[1], entry["color"], files)
            data["tiles"][eid] = {"file": name, "color": entry["color"],
                                  "url": f"/local/scene_tiles/{name}?v={int(time.time())}"}
            await hass.async_add_executor_job(_write_package, data["tiles"])
            await hass.services.async_call("homeassistant", "reload_core_config", blocking=True)
            await hass.services.async_call("homeassistant", "update_entity", {"entity_id": [eid]}, blocking=True)
            _LOGGER.info("room_scenes: solid tile %s (%s) attached to %s", name, entry["color"], eid)
            changed = True
        elif not solid and entry.get("icon") in (None, "mdi:circle"):
            entry["icon"] = DEFAULT_ICON
            changed = True
        if not entry.get("icon"):
            entry["icon"] = DEFAULT_ICON
            changed = True
        return changed

    async def _catch_up(_event=None):
        # At startup (states loaded): swatch without colour, or "mdi:circle" without picture
        # (= white dot). Swatches that are already fine are never touched.
        done = []
        for s in data["scenes"]:
            st = hass.states.get(s["entity"])
            no_picture = not (st and st.attributes.get("entity_picture"))
            if s.get("color") and not (s.get("icon") == "mdi:circle" and no_picture):
                continue
            try:
                if await _dress(s):
                    done.append(f"{s['entity']}={s.get('color')}")
            except Exception as err:  # an unreadable scene must not block the list
                _LOGGER.warning("room_scenes: swatch of %s not processed: %s", s["entity"], err)
        if done:
            await hass.async_add_executor_job(_save, data)
            _publish()
            _LOGGER.info("room_scenes: swatches caught up: %s", ", ".join(done))

    def _publish():
        selected_state = hass.states.get(MANAGE_SELECT_ENTITY)
        selected_option = selected_state.state if selected_state else None

        rows = []
        for i, s in enumerate(data["scenes"]):
            select_value = f"{s['title']} || {s['entity']}"
            is_selected = selected_option == select_value
            # Custom `scene-select-row` (www/scene-select-row.js): same rendering as the native
            # scene row (picture + name + button), but the button SELECTS the scene in the
            # manage dropdown instead of activating it.
            # Rejected: `type: button`, the row loses the scene picture.
            rows.append({
                "type": "custom:scene-select-row",
                "action_name": "Select",
                "entity": s["entity"],
                "name": f"✅ {s['title']}" if is_selected else s["title"],
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "input_select.select_option",
                    "target": {"entity_id": MANAGE_SELECT_ENTITY},
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
                "friendly_name": "Room scenes - list",
                "icon": "mdi:palette-swatch",
            },
            # force_update=True is required: `rows` and `list` are rebuilt from data["scenes"],
            # which the services modify in place, so the old and new states can hold the same
            # objects. Without force_update, HA sees equal attributes and drops the update, with
            # no error and no log: the UI never changes. Do not remove.
            force_update=True,
        )

    _publish()

    if hass.state is CoreState.running:
        hass.async_create_task(_catch_up())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _catch_up)

    async def _on_selection_changed(event):
        _publish()

    async_track_state_change_event(hass, [MANAGE_SELECT_ENTITY], _on_selection_changed)

    async def handle_add(call: ServiceCall):
        entity_id = call.data["entity_id"]
        title = call.data.get("title")
        icon = call.data.get("icon")
        color = call.data.get("color")
        position = call.data.get("position")

        if not entity_id.startswith("scene."):
            raise ValueError(f"{entity_id} is not a scene.* entity")
        state = hass.states.get(entity_id)
        if state is None:
            raise ValueError(f"{entity_id} does not exist")
        if not title:
            title = state.attributes.get("friendly_name", entity_id)

        data["scenes"] = [s for s in data["scenes"] if s["entity"] != entity_id]
        entry = {"entity": entity_id, "title": title}
        if icon:
            entry["icon"] = icon
        if color:
            entry["color"] = color
        try:
            await _dress(entry)
        except Exception as err:  # the entry is still added, just without a computed colour
            _LOGGER.warning("room_scenes: swatch of %s not dressed: %s", entity_id, err)
        entry.setdefault("icon", DEFAULT_ICON)
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

        # The exact state of each light (on or off, xy colour, brightness), without the group.
        lights = [e for e in data["lights"] if e != GROUP_ENTITY]
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
            # APPEND to scenes.yaml: the rest of the file (and its comments) is not rewritten.
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
            await _dress(entry, entities_block)
        except Exception as err:  # the scene is captured no matter what
            _LOGGER.warning("room_scenes: swatch of %s not dressed: %s", new_entity_id, err)
        entry.setdefault("icon", DEFAULT_ICON)
        data["scenes"].append(entry)
        await hass.async_add_executor_job(_save, data)
        _publish()
        _LOGGER.info("room_scenes: current light state captured as %s", new_entity_id)

    hass.services.async_register(DOMAIN, "add", handle_add, schema=ADD_SCHEMA)
    hass.services.async_register(DOMAIN, "remove", handle_remove, schema=REMOVE_SCHEMA)
    hass.services.async_register(DOMAIN, "rename", handle_rename, schema=RENAME_SCHEMA)
    hass.services.async_register(DOMAIN, "move", handle_move, schema=MOVE_SCHEMA)
    hass.services.async_register(DOMAIN, "capture", handle_capture, schema=CAPTURE_SCHEMA)

    return True
