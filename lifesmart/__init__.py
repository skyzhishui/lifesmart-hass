"""LifeSmart integration for Home Assistant.

by @skyzhishui

Adapted for Home Assistant 2025+/2026:

- async_setup imports YAML into a config entry; config flow adds UI setup
- dual mode: mode=online uses the cloud API, mode=offline talks to the
  station over LAN (TCP 8888, see local.py); optional offline->online
  fallback keeps control working when the station is unreachable
- websocket-client >= 1.x callback signatures (on_close/ws, code, msg)
- ws io messages are dispatched to entity objects registered in hass.data
  (no more hass.states.set hacks, no NoneType crashes)
- HA stop event stops the websocket / local keep-alive threads
- blocking cloud calls run in the executor, all with a request timeout

Cloud API reference: docs/lifesmart/LifeSmart 云平台服务接口.pdf (v1.38)
- REST  https://api.ilifesmart.com/app/api.*  (sign = md5 of sorted params)
- WSS   wss://api.ilifesmart.com:8443/wsapp/  (WbAuth, push type="io")
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import urllib.request
from typing import Any

import voluptuous as vol
import websocket

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import ConfigType

from .local import LifeSmartLocalManager

_LOGGER = logging.getLogger(__name__)

DOMAIN = "lifesmart"
LIFESMART_STATE_MANAGER = "lifesmart_wss"

CONF_LIFESMART_APPKEY = "appkey"
CONF_LIFESMART_APPTOKEN = "apptoken"
CONF_LIFESMART_USERTOKEN = "usertoken"
CONF_LIFESMART_USERID = "userid"
CONF_LIFESMART_MODE = "mode"
CONF_LIFESMART_FALLBACK = "fallback"
CONF_LIFESMART_LOCAL_USERNAME = "local_username"
CONF_LIFESMART_LOCAL_PASSWORD = "local_password"
CONF_EXCLUDE_ITEMS = "exclude"

MODE_ONLINE = "online"
MODE_OFFLINE = "offline"

PLATFORMS = ["switch", "light", "climate", "cover", "sensor", "binary_sensor"]

SWTICH_TYPES = ["SL_SF_RC",
"SL_SW_RC",
"SL_SW_IF3",
"SL_SF_IF3",
"SL_SW_CP3",
"SL_SW_RC3",
"SL_SW_IF2",
"SL_SF_IF2",
"SL_SW_CP2",
"SL_SW_FE2",
"SL_SW_RC2",
"SL_SW_ND2",
"SL_MC_ND2",
"SL_SW_IF1",
"SL_SF_IF1",
"SL_SW_CP1",
"SL_SW_FE1",
"SL_OL_W",
"SL_SW_RC1",
"SL_SW_ND1",
"SL_MC_ND1",
"SL_SW_ND3",
"SL_MC_ND3",
"SL_SW_ND2",
"SL_MC_ND2",
"SL_SW_ND1",
"SL_MC_ND1",
"SL_S",
"SL_SPWM",
"SL_P_SW",
"SL_SW_DM1",
"SL_SW_MJ2",
"SL_SW_MJ1",
"SL_OL",
"SL_OL_3C",
"SL_OL_DE",
"SL_OL_UK",
"SL_OL_UL",
"OD_WE_OT1",
"SL_NATURE"
]
LIGHT_SWITCH_TYPES = ["SL_OL_W",
"SL_SW_IF1",
"SL_SW_IF2",
"SL_SW_IF3",
]
QUANTUM_TYPES=["OD_WE_QUAN",
]

SPOT_TYPES = ["MSL_IRCTL",
"OD_WE_IRCTL",
"SL_SPOT"]
BINARY_SENSOR_TYPES = ["SL_SC_G",
"SL_SC_BG",
"SL_SC_MHW ",
"SL_SC_BM",
"SL_SC_CM",
"SL_P_A"]
COVER_TYPES = ["SL_DOOYA"]
GAS_SENSOR_TYPES = ["SL_SC_WA ",
"SL_SC_CH",
"SL_SC_CP",
"ELIQ_EM"]
EV_SENSOR_TYPES = ["SL_SC_THL",
"SL_SC_BE",
"SL_SC_CQ"]
OT_SENSOR_TYPES = ["SL_SC_MHW",
"SL_SC_BM",
"SL_SC_G",
"SL_SC_BG"]
LOCK_TYPES = ["SL_LK_LS",
"SL_LK_GTM",
"SL_LK_AG",
"SL_LK_SG",
"SL_LK_YL"]

SPEED_OFF = "Speed_Off"
SPEED_LOW = "Speed_Low"
SPEED_MEDIUM = "Speed_Medium"
SPEED_HIGH = "Speed_High"

LIFESMART_STATE_LIST = ["off",
"auto",
"fan_only",
"cool",
"heat",
"dry"]

CLIMATE_TYPES = ["V_AIR_P",
"SL_CP_DN"]

ENTITYID = 'entity_id'

LIFESMART_WSS_URL = "wss://api.ilifesmart.com:8443/wsapp/"
REQUEST_TIMEOUT = 15

SEND_KEYS_SCHEMA = vol.Schema(
    {
        vol.Required("agt"): cv.string,
        vol.Required("me"): cv.string,
        vol.Required("ai"): cv.string,
        vol.Required("category"): cv.string,
        vol.Required("brand"): cv.string,
        vol.Required("keys"): vol.Any(str, list),
    }
)

SEND_ACKEYS_SCHEMA = SEND_KEYS_SCHEMA.extend(
    {
        vol.Required("power"): vol.Any(int, str),
        vol.Required("mode"): vol.Any(int, str),
        vol.Required("temp"): vol.Any(int, str),
        vol.Required("wind"): vol.Any(int, str),
        vol.Required("swing"): vol.Any(int, str),
    }
)


def _validate_mode(cfg):
    """Check mode-dependent requirements (cloud creds / local host)."""
    mode = cfg.get(CONF_LIFESMART_MODE, MODE_ONLINE)
    if mode not in (MODE_ONLINE, MODE_OFFLINE):
        raise vol.Invalid(f"mode must be '{MODE_ONLINE}' or '{MODE_OFFLINE}'")
    if mode == MODE_ONLINE or cfg.get(CONF_LIFESMART_FALLBACK, False):
        if not cfg.get(CONF_LIFESMART_APPKEY) or not cfg.get(CONF_LIFESMART_APPTOKEN):
            raise vol.Invalid(
                "appkey/apptoken are required for online mode or fallback"
            )
        if not cfg.get(CONF_USERNAME) or not cfg.get(CONF_PASSWORD):
            raise vol.Invalid(
                "username/password are the cloud account; they are required "
                "for online mode or fallback"
            )
    if mode == MODE_OFFLINE:
        if not cfg.get(CONF_HOST):
            raise vol.Invalid("host is required for offline mode")
        if not cfg.get(CONF_LIFESMART_LOCAL_USERNAME) \
                or not cfg.get(CONF_LIFESMART_LOCAL_PASSWORD):
            raise vol.Invalid(
                "local_username/local_password are the station admin "
                "credentials and are required for offline mode"
            )
    return cfg


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            vol.Schema(
                {
                    vol.Optional(CONF_HOST): cv.string,
                    vol.Optional(CONF_LIFESMART_MODE, default=MODE_ONLINE): cv.string,
                    vol.Optional(CONF_LIFESMART_FALLBACK, default=False): cv.boolean,
                    vol.Optional(CONF_LIFESMART_APPKEY): cv.string,
                    vol.Optional(CONF_LIFESMART_APPTOKEN): cv.string,
                    vol.Optional(CONF_LIFESMART_LOCAL_USERNAME): cv.string,
                    vol.Optional(CONF_LIFESMART_LOCAL_PASSWORD): cv.string,
                    vol.Optional(CONF_USERNAME): cv.string,
                    vol.Optional(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_LIFESMART_USERTOKEN): cv.string,
                    vol.Optional(CONF_LIFESMART_USERID): cv.string,
                    vol.Optional(CONF_EXCLUDE_ITEMS, default=[]): vol.All(
                        cv.ensure_list, [cv.string]
                    ),
                }
            ),
            _validate_mode,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _api_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON payload to the LifeSmart cloud API."""
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sign(method: str, params: list[tuple[str, Any]], usertoken: str, userid: str,
          appkey: str, apptoken: str) -> tuple[int, str]:
    """Build (time, sign) for a LifeSmart API call."""
    tick = int(time.time())
    parts = [f"method:{method}"]
    parts += [f"{k}:{v}" for k, v in params]
    parts += [
        f"time:{tick}",
        f"userid:{userid}",
        f"usertoken:{usertoken}",
        f"appkey:{appkey}",
        f"apptoken:{apptoken}",
    ]
    sdata = ",".join(parts)
    return tick, hashlib.md5(sdata.encode("UTF-8")).hexdigest()


def _system_block(tick: int, sign: str, userid: str, appkey: str) -> dict[str, Any]:
    return {
        "ver": "1.0",
        "lang": "en",
        "userid": userid,
        "appkey": appkey,
        "time": tick,
        "sign": sign,
    }


def lifesmart_EpGetAll(appkey, apptoken, usertoken, userid):
    """Fetch all devices (eps) of the account."""
    tick, sign = _sign("EpGetAll", [], usertoken, userid, appkey, apptoken)
    send_values = {
        "id": 1,
        "method": "EpGetAll",
        "system": _system_block(tick, sign, userid, appkey),
    }
    response = _api_post("https://api.ilifesmart.com/app/api.EpGetAll", send_values)
    if response["code"] == 0:
        return response["message"]
    return False


def lifesmart_Sendkeys(appkey, apptoken, usertoken, userid, agt, ai, me, category, brand, keys):
    """Send IR keys via irapi.SendKeys."""
    params = [
        ("agt", agt), ("ai", ai), ("brand", brand), ("category", category),
        ("keys", keys), ("me", me),
    ]
    tick, sign = _sign("SendKeys", params, usertoken, userid, appkey, apptoken)
    _LOGGER.debug("sendkey: method:SendKeys%s", sdata_debug(params, tick, userid, appkey, apptoken))
    send_values = {
        "id": 1,
        "method": "SendKeys",
        "params": {"agt": agt, "me": me, "category": category, "brand": brand,
                   "ai": ai, "keys": keys},
        "system": _system_block(tick, sign, userid, appkey),
    }
    response = _api_post("https://api.ilifesmart.com/app/irapi.SendKeys", send_values)
    _LOGGER.debug("sendkey_res: %s", response)
    return response


def sdata_debug(params, tick, userid, appkey, apptoken):
    """Render the sign source string for debug logs."""
    return ",".join([f"{k}:{v}" for k, v in params]) + \
        f",time:{tick},userid:{userid},appkey:{appkey},apptoken:{apptoken}"


def lifesmart_Sendackeys(appkey, apptoken, usertoken, userid, agt, ai, me, category,
                         brand, keys, power, mode, temp, wind, swing):
    """Send AC IR keys via irapi.SendACKeys."""
    params = [
        ("agt", agt), ("ai", ai), ("brand", brand), ("category", category),
        ("keys", keys), ("me", me), ("mode", mode), ("power", power),
        ("swing", swing), ("temp", temp), ("wind", wind),
    ]
    tick, sign = _sign("SendACKeys", params, usertoken, userid, appkey, apptoken)
    _LOGGER.debug("sendackey: method:SendACKeys%s", sdata_debug(params, tick, userid, appkey, apptoken))
    send_values = {
        "id": 1,
        "method": "SendACKeys",
        "params": {"agt": agt, "me": me, "category": category, "brand": brand,
                   "ai": ai, "keys": keys, "power": power, "mode": mode,
                   "temp": temp, "wind": wind, "swing": swing},
        "system": _system_block(tick, sign, userid, appkey),
    }
    response = _api_post("https://api.ilifesmart.com/app/irapi.SendACKeys", send_values)
    _LOGGER.debug("sendackey_res: %s", response)
    return response


def lifesmart_Login(uid, pwd, appkey):
    """Login with uid/pwd to get a token (legacy auth flow)."""
    login_data = {"uid": uid, "pwd": pwd, "appkey": appkey}
    return _api_post("https://api.ilifesmart.com/app/auth.login", login_data)


def lifesmart_doAuth(userid, token, appkey):
    """Exchange the login token for a usertoken."""
    auth_data = {"userid": userid, "token": token, "appkey": appkey, "rgn": "cn"}
    return _api_post("https://api.ilifesmart.com/app/auth.do_auth", auth_data)


def get_fan_mode(_fanspeed):
    """Map a numeric LifeSmart fan speed to a HA fan mode."""
    fanmode = None
    if _fanspeed < 30:
        fanmode = SPEED_LOW
    elif _fanspeed < 65 and _fanspeed >= 30:
        fanmode = SPEED_MEDIUM
    elif _fanspeed >= 65:
        fanmode = SPEED_HIGH
    return fanmode


async def _async_dispatch(hass: HomeAssistant, msg: dict[str, Any]) -> None:
    """Handle one websocket io message on the event loop."""
    store = hass.data.get(DOMAIN)
    if store is None:
        return
    m = msg.get("msg") or {}
    idx = m.get("idx")
    if idx == "s":
        # device level info (name/stat/fullcls changes), no IO state change
        return
    devtype = m.get("devtype")
    agt = str(m.get("agt", "")).replace("_", "")
    me = m.get("me")
    entities = store["entities"]
    entity = entities.get((devtype, agt, me, idx))
    if entity is None:
        # climate entities register one key per device (any idx)
        entity = entities.get((devtype, agt, me, None))
    if entity is None:
        _LOGGER.debug("lifesmart: ws message for unknown entity: %s", m)
        return
    try:
        entity.handle_ws_update(m)
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("lifesmart: ws update failed for %s: %s", entity.entity_id, err)
        return
    entity.async_write_ha_state()


def _cloud_login(param: dict[str, Any]) -> None:
    """Run the legacy cloud login/do_auth flow (blocking, executor)."""
    res_login = lifesmart_Login(param["username"], param["passwd"],
                                param["appkey"])
    if res_login.get("code") == "error":
        raise ConfigEntryAuthFailed(f"lifesmart login fail: {res_login.get('message')}")
    param["token"] = res_login["token"]
    param["userid"] = res_login["userid"]
    res_doauth = lifesmart_doAuth(param["userid"], param["token"], param["appkey"])
    if res_doauth.get("code") == "error":
        raise ConfigEntryAuthFailed(f"lifesmart do_auth fail: {res_doauth.get('message')}")
    param["usertoken"] = res_doauth["usertoken"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the lifesmart component: YAML drives the imported entry."""
    if DOMAIN not in config:
        return True
    entries = hass.config_entries.async_entries(DOMAIN)
    imported = [e for e in entries if e.source == "import"]
    if imported:
        # YAML stays the source of truth for the imported entry
        if imported[0].data != config[DOMAIN]:
            hass.config_entries.async_update_entry(
                imported[0], data=config[DOMAIN]
            )
            hass.async_create_task(
                hass.config_entries.async_reload(imported[0].entry_id)
            )
    elif not entries:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=config[DOMAIN],
            )
        )
    return True


def _platforms_for(devtype: str) -> list[str]:
    """Return the HA platforms serving this devtype (legacy routing kept)."""
    if devtype in SWTICH_TYPES:
        plats = ["switch"]
    elif devtype in BINARY_SENSOR_TYPES:
        plats = ["binary_sensor"]
    elif devtype in COVER_TYPES:
        plats = ["cover"]
    elif devtype in SPOT_TYPES:
        plats = ["light"]
    elif devtype in CLIMATE_TYPES:
        plats = ["climate"]
    elif devtype in GAS_SENSOR_TYPES or devtype in EV_SENSOR_TYPES:
        plats = ["sensor"]
    else:
        plats = []
    if devtype in OT_SENSOR_TYPES:
        plats.append("sensor")
    if devtype in LIGHT_SWITCH_TYPES:
        plats.append("light")
    return list(dict.fromkeys(plats))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LifeSmart from a config entry (UI or YAML import)."""
    cfg = entry.data
    param: dict[str, Any] = {
        "appkey": cfg.get(CONF_LIFESMART_APPKEY),
        "apptoken": cfg.get(CONF_LIFESMART_APPTOKEN),
        "username": cfg.get(CONF_USERNAME),
        "passwd": cfg.get(CONF_PASSWORD),
        "local_username": cfg.get(CONF_LIFESMART_LOCAL_USERNAME),
        "local_password": cfg.get(CONF_LIFESMART_LOCAL_PASSWORD),
        "usertoken": cfg.get(CONF_LIFESMART_USERTOKEN),
        "userid": cfg.get(CONF_LIFESMART_USERID),
        "host": cfg.get(CONF_HOST),
        "mode": cfg.get(CONF_LIFESMART_MODE, MODE_ONLINE),
        "fallback": cfg.get(CONF_LIFESMART_FALLBACK, False),
    }
    exclude_items = set(cfg.get(CONF_EXCLUDE_ITEMS, []))
    _LOGGER.info("lifesmart: setting up entry (mode=%s)", param["mode"])
    hass.data[DOMAIN] = {"param": param, "exclude": exclude_items, "entities": {}}

    # -- fetch the device list (cloud EpGetAll or local get-config) --
    if param["mode"] == MODE_OFFLINE:
        devices = await _async_start_local(hass, param)
    else:
        devices = await _async_start_cloud(hass, param)
    if devices is False:
        raise ConfigEntryNotReady("lifesmart EpGetAll returned an error response")

    # -- bucket devices per target platform (legacy routing kept) --
    platform_devices: dict[str, list[dict[str, Any]]] = {}
    for dev in devices:
        if dev.get("me") in exclude_items:
            continue
        if not isinstance(dev.get("agt"), str) or not isinstance(dev.get("devtype"), str):
            _LOGGER.warning("lifesmart: skipping malformed device: %s", dev)
            continue
        dev["agt"] = dev["agt"].replace("_", "")
        for plat in _platforms_for(dev["devtype"]):
            platform_devices.setdefault(plat, []).append(dev)
    hass.data[DOMAIN]["platform_devices"] = platform_devices

    # -- load platforms via the standard entry-forwarded path --
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # -- services --
    async def async_send_keys(call: ServiceCall) -> None:
        """Handle lifesmart.send_keys service call."""
        local = hass.data[DOMAIN].get("local_manager")
        if param["mode"] == MODE_OFFLINE and local and local.is_connected():
            # local mode: the ai IS the station scene name (AI_IR_...)
            await hass.async_add_executor_job(local.run_scene, call.data["ai"])
            return
        if not param["appkey"] or not param.get("usertoken"):
            _LOGGER.warning("lifesmart.send_keys: cloud unavailable in offline mode")
            return
        await hass.async_add_executor_job(
            lifesmart_Sendkeys,
            param["appkey"], param["apptoken"], param["usertoken"], param["userid"],
            call.data["agt"], call.data["me"], call.data["ai"],
            call.data["category"], call.data["brand"], call.data["keys"],
        )

    async def async_send_ackeys(call: ServiceCall) -> None:
        """Handle lifesmart.send_ackeys service call (cloud only)."""
        if not param["appkey"] or not param.get("usertoken"):
            _LOGGER.warning("lifesmart.send_ackeys: cloud unavailable in offline mode")
            return
        await hass.async_add_executor_job(
            lifesmart_Sendackeys,
            param["appkey"], param["apptoken"], param["usertoken"], param["userid"],
            call.data["agt"], call.data["me"], call.data["ai"],
            call.data["category"], call.data["brand"], call.data["keys"],
            call.data["power"], call.data["mode"], call.data["temp"],
            call.data["wind"], call.data["swing"],
        )

    hass.services.async_register(DOMAIN, "send_keys", async_send_keys, schema=SEND_KEYS_SCHEMA)
    hass.services.async_register(DOMAIN, "send_ackeys", async_send_ackeys, schema=SEND_ACKEYS_SCHEMA)

    # -- stop all keep-alive threads when HA stops --
    async def _async_stop_managers(event):
        """Stop the local/websocket threads when HA stops."""
        for name in ("manager", "local_manager", "cloud_manager"):
            mgr = hass.data[DOMAIN].get(name)
            if mgr:
                await hass.async_add_executor_job(mgr.stop_keep_alive)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_managers)
    return True


async def _async_start_cloud(hass: HomeAssistant, param: dict[str, Any]) -> list:
    """Authenticate against the cloud, fetch devices, start the wss thread."""
    # -- authentication (legacy login/do_auth flow, proven working) --
    try:
        await hass.async_add_executor_job(_cloud_login, param)
    except ConfigEntryAuthFailed:
        raise
    except (OSError, ValueError) as err:
        _LOGGER.error("lifesmart login request failed: %s", err)
        raise ConfigEntryNotReady("lifesmart login request failed") from err

    # -- fetch device list --
    try:
        devices = await hass.async_add_executor_job(
            lifesmart_EpGetAll,
            param["appkey"], param["apptoken"], param["usertoken"], param["userid"],
        )
    except (OSError, ValueError) as err:
        _LOGGER.error("lifesmart EpGetAll request failed: %s", err)
        raise ConfigEntryNotReady("lifesmart EpGetAll request failed") from err

    # -- websocket realtime push --
    def on_message(_ws, message):
        """Handle incoming websocket frames (runs in the ws thread)."""
        try:
            msg = json.loads(message)
        except (TypeError, ValueError):
            _LOGGER.debug("lifesmart: non-json websocket message: %s", message)
            return
        if msg.get("type") != "io":
            _LOGGER.debug("lifesmart: websocket message: %s", msg)
            return
        try:
            hass.loop.call_soon_threadsafe(
                hass.async_create_task, _async_dispatch(hass, msg)
            )
        except RuntimeError:
            # event loop already closed during shutdown
            pass

    def on_error(_ws, error):
        _LOGGER.info("lifesmart websocket_error: %s", error)

    def on_close(_ws, close_status_code, close_msg):
        _LOGGER.debug(
            "lifesmart websocket closed: %s %s", close_status_code, close_msg
        )

    def on_open(ws):
        """Authenticate the websocket with WbAuth on connect."""
        tick = int(time.time())
        sdata = (
            "method:WbAuth,time:" + str(tick)
            + ",userid:" + param["userid"]
            + ",usertoken:" + param["usertoken"]
            + ",appkey:" + param["appkey"]
            + ",apptoken:" + param["apptoken"]
        )
        sign = hashlib.md5(sdata.encode("UTF-8")).hexdigest()
        send_values = {
            "id": 1,
            "method": "WbAuth",
            "system": _system_block(tick, sign, param["userid"], param["appkey"]),
        }
        ws.send(json.dumps(send_values))
        _LOGGER.info("lifesmart websocket sending_data...")

    def build_ws_manager():
        """Build the keep-alive websocket manager with current creds."""
        ws = websocket.WebSocketApp(
            LIFESMART_WSS_URL,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.on_open = on_open
        return LifeSmartStatesManager(ws=ws)

    manager = build_ws_manager()
    hass.data[DOMAIN]["manager"] = manager
    hass.data[DOMAIN]["build_ws_manager"] = build_ws_manager
    manager.start_keep_alive()
    return devices


async def _async_start_local(hass: HomeAssistant, param: dict[str, Any]) -> list:
    """Connect to the station over LAN, fetch devices, keep the session up."""
    loop = hass.loop

    def _to_loop(target):
        """Wrap a thread-side callback onto the event loop."""
        def inner(*args):
            try:
                loop.call_soon_threadsafe(hass.async_create_task, target(*args))
            except RuntimeError:
                pass
        return inner

    got_devices = asyncio.Event()
    holder: dict[str, Any] = {}

    async def _async_on_devices(devices):
        holder["devices"] = devices
        got_devices.set()

    async def _async_on_state(up):
        await _async_apply_fallback(hass, param, up)

    local = LifeSmartLocalManager(
        param["host"], param.get("local_username"), param.get("local_password"),
        on_devices=_to_loop(_async_on_devices),
        on_push=_to_loop(lambda msg: _async_dispatch(hass, msg)),
        on_state=_to_loop(_async_on_state),
    )
    hass.data[DOMAIN]["local_manager"] = local
    local.start_keep_alive()
    try:
        await asyncio.wait_for(got_devices.wait(), timeout=25)
    except asyncio.TimeoutError as err:
        await hass.async_add_executor_job(local.stop_keep_alive)
        raise ConfigEntryNotReady(
            f"lifesmart local: no reply from station {param['host']}"
        ) from err
    param["agt"] = local.node
    _LOGGER.info("lifesmart local: %d devices from station %s",
                 len(holder["devices"]), param["host"])
    return holder["devices"]


async def _async_apply_fallback(hass: HomeAssistant, param: dict[str, Any], up: bool) -> None:
    """Offline->online fallback: start cloud when local drops, stop on return."""
    if not param["fallback"] or not param["appkey"]:
        return
    store = hass.data.get(DOMAIN)
    if store is None:
        return
    cloud = store.get("cloud_manager")
    if up:
        if cloud:
            _LOGGER.info("lifesmart local: station back, stopping cloud fallback")
            store["cloud_manager"] = None
            try:
                await hass.async_add_executor_job(cloud.stop_keep_alive)
            except Exception as err:  # noqa: BLE001 - best effort cleanup
                _LOGGER.debug("lifesmart: fallback stop failed: %s", err)
        return
    if cloud or param["mode"] != MODE_OFFLINE:
        return
    _LOGGER.warning("lifesmart local: station %s unreachable, falling back to cloud",
                    param["host"])
    try:
        await hass.async_add_executor_job(_cloud_login, param)
    except (ConfigEntryAuthFailed, OSError, ValueError) as err:
        _LOGGER.error("lifesmart fallback: cloud login failed: %s", err)
        return
    build = store.get("build_ws_manager")
    manager = build() if build else None
    if manager:
        store["cloud_manager"] = manager
        manager.start_keep_alive()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry: stop threads, unload platforms, remove services."""
    # stop push threads first so no update lands during entity teardown
    store = hass.data.get(DOMAIN)
    if store:
        for name in ("manager", "local_manager", "cloud_manager"):
            mgr = store.get(name)
            if mgr:
                await hass.async_add_executor_job(mgr.stop_keep_alive)
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.services.async_remove(DOMAIN, "send_keys")
    hass.services.async_remove(DOMAIN, "send_ackeys")
    hass.data[DOMAIN] = {}
    return True


class LifeSmartEntity(Entity):
    """LifeSmart base device."""

    _attr_should_poll = False
    _attr_assumed_state = False

    def __init__(self, dev, idx, val, param):
        """Initialize the entity."""
        self._name = dev["name"] + "_" + str(idx)
        self._appkey = param.get("appkey")
        self._apptoken = param.get("apptoken")
        self._usertoken = param.get("usertoken")
        self._userid = param.get("userid")
        self._agt = dev["agt"]
        self._me = dev["me"]
        self._idx = idx
        self._devtype = dev["devtype"]
        self._attributes = {
            "agt": self._agt,
            "me": self._me,
            "idx": idx,
            "devtype": self._devtype,
        }
        self._entity_key = (self._devtype, self._agt, self._me, idx)
        self._attr_unique_id = f"{self._agt}_{self._me}_{idx}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._agt}_{self._me}")},
            name=dev["name"],
            manufacturer="LifeSmart",
            model=self._devtype,
        )

    @property
    def name(self):
        """Return LifeSmart device name."""
        return self._name

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        return self._attributes

    async def async_added_to_hass(self):
        """Register this entity for websocket updates."""
        self.hass.data[DOMAIN]["entities"][self._entity_key] = self

    async def async_will_remove_from_hass(self):
        """Unregister this entity."""
        self.hass.data[DOMAIN]["entities"].pop(self._entity_key, None)

    def handle_ws_update(self, msg):
        """Update internal state from a websocket io message."""
        raise NotImplementedError

    def _lifesmart_epset(self, ep_type, val, idx):
        """Send one EpSet command via the active transport (local first)."""
        store = self.hass.data[DOMAIN]
        local = store.get("local_manager")
        if store["param"]["mode"] == MODE_OFFLINE and local \
                and local.is_connected():
            return local.epset(self._agt, self._me, idx, ep_type, val)
        if not self._appkey or not self._usertoken:
            _LOGGER.error(
                "lifesmart epset %s/%s: no cloud transport (offline mode?)",
                self._me, idx,
            )
            return -1
        url = "https://api.ilifesmart.com/app/api.EpSet"
        params = [
            ("agt", self._agt), ("idx", idx), ("me", self._me),
            ("type", ep_type), ("val", val),
        ]
        tick, sign = _sign("EpSet", params, self._usertoken, self._userid,
                           self._appkey, self._apptoken)
        send_values = {
            "id": 1,
            "method": "EpSet",
            "system": _system_block(tick, sign, self._userid, self._appkey),
            "params": {
                "agt": self._agt,
                "me": self._me,
                "idx": idx,
                "type": ep_type,
                "val": val,
            },
        }
        _LOGGER.debug("epset_send: %s", json.dumps(send_values))
        response = _api_post(url, send_values)
        _LOGGER.debug("epset_res: %s", response)
        return response.get("code", -1)

    def _lifesmart_epget(self):
        """Send an api.EpGet query to the cloud."""
        url = "https://api.ilifesmart.com/app/api.EpGet"
        params = [("agt", self._agt), ("me", self._me)]
        tick, sign = _sign("EpGet", params, self._usertoken, self._userid,
                           self._appkey, self._apptoken)
        send_values = {
            "id": 1,
            "method": "EpGet",
            "system": _system_block(tick, sign, self._userid, self._appkey),
            "params": {"agt": self._agt, "me": self._me},
        }
        response = _api_post(url, send_values)
        return response["message"]["data"]


class LifeSmartStatesManager(threading.Thread):
    """Keep the LifeSmart websocket alive with automatic reconnection."""

    def __init__(self, ws):
        """Init LifeSmart Update Manager."""
        super().__init__(name="lifesmart-wss", daemon=True)
        self._run = threading.Event()
        self._ws = ws

    def run(self):
        """Connect and reconnect forever until stopped."""
        while self._run.is_set():
            _LOGGER.info("lifesmart: starting wss...")
            try:
                self._ws.run_forever()
            except Exception as err:  # noqa: BLE001 - never let the thread die
                _LOGGER.error("lifesmart websocket crashed: %s", err)
            if not self._run.is_set():
                break
            _LOGGER.info("lifesmart: wss closed, restarting in 10s...")
            self._run.wait(10)

    def start_keep_alive(self):
        """Start keep alive mechanism."""
        self._run.set()
        self.start()

    def stop_keep_alive(self):
        """Stop keep alive mechanism."""
        self._run.clear()
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.join(timeout=15)
