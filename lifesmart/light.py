"""Support for LifeSmart lights."""
import binascii
import logging
import struct

import homeassistant.util.color as color_util
from homeassistant.components.light import (
    ATTR_HS_COLOR,
    ColorMode,
    ENTITY_ID_FORMAT,
    LightEntity,
    LightEntityFeature,
)

from . import DOMAIN, LifeSmartEntity, _api_post, _sign, _system_block

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Perform the setup for LifeSmart devices."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("light", []):
        devices += [
            LifeSmartLight(dev, idx, dev["data"][idx], store["param"])
            for idx in dev.get("data", {})
            if idx in ("RGB", "RGBW")
        ]
    async_add_entities(devices)


class LifeSmartLight(LifeSmartEntity, LightEntity):
    """Representation of a LifeSmartLight."""

    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS

    def __init__(self, dev, idx, val, param):
        """Initialize the LifeSmartLight."""
        super().__init__(dev, idx, val, param)
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"] + "_" + idx).lower()
        )
        self._state = val["type"] % 2 == 1
        value = val["val"]
        if value == 0:
            self._hs = None
        else:
            rgbhexstr = "%x" % value
            rgbhexstr = rgbhexstr.zfill(8)
            rgbhex = bytes.fromhex(rgbhexstr)
            rgba = struct.unpack("BBBB", rgbhex)
            rgb = rgba[1:]
            self._hs = color_util.color_RGB_to_hs(*rgb)
            _LOGGER.debug("lifesmart light %s hs: %s", self.entity_id, self._hs)

    async def async_added_to_hass(self):
        """Add to hass and fetch the IR remote list of the hub."""
        await super().async_added_to_hass()
        if not self._appkey or not self._usertoken:
            # IR remote list queries are cloud-only (online mode)
            return
        try:
            rmlist = await self.hass.async_add_executor_job(
                self._lifesmart_GetRemoteList
            )
        except (OSError, ValueError, KeyError) as err:
            _LOGGER.warning(
                "lifesmart %s: GetRemoteList failed: %s", self.entity_id, err
            )
            return
        if not isinstance(rmlist, dict):
            return
        rmdata = {}
        for ai in rmlist:
            try:
                rms = await self.hass.async_add_executor_job(
                    self._lifesmart_GetRemotes, ai
                )
                rms["category"] = rmlist[ai].get("category")
                rms["brand"] = rmlist[ai].get("brand")
                rmdata[ai] = rms
            except (OSError, ValueError, KeyError, TypeError) as err:
                _LOGGER.warning(
                    "lifesmart %s: GetRemote(%s) failed: %s", self.entity_id, ai, err
                )
                continue
        if rmdata:
            self._attributes.setdefault("remotelist", rmdata)
            self.async_write_ha_state()

    @property
    def is_on(self):
        """Return true if it is on."""
        return self._state

    @property
    def hs_color(self):
        """Return the hs color value."""
        return self._hs

    @property
    def effect_list(self):
        """Return the list of supported IR remotes as effects."""
        rmdata = self._attributes.get("remotelist")
        return list(rmdata) if rmdata else None

    @property
    def supported_features(self):
        """Return the supported features."""
        if self._attributes.get("remotelist"):
            return LightEntityFeature.EFFECT
        return LightEntityFeature(0)

    def turn_on(self, **kwargs):
        """Turn the light on."""
        if ATTR_HS_COLOR in kwargs:
            self._hs = kwargs[ATTR_HS_COLOR]
        if self._hs is None:
            # no known color yet: just power the light on
            if self._lifesmart_epset("0x81", 1, self._idx) == 0:
                self._state = True
                self.schedule_update_ha_state()
            return
        rgb = color_util.color_hs_to_RGB(*self._hs)
        rgba = (0,) + rgb
        rgbhex = binascii.hexlify(struct.pack("BBBB", *rgba)).decode("ASCII")
        rgbhex = int(rgbhex, 16)
        if self._lifesmart_epset("0xff", rgbhex, self._idx) == 0:
            self._state = True
            self.schedule_update_ha_state()

    def turn_off(self, **kwargs):
        """Turn the light off."""
        if self._lifesmart_epset("0x80", 0, self._idx) == 0:
            self._state = False
            self.schedule_update_ha_state()

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        self._state = msg["type"] % 2 == 1

    def _lifesmart_GetRemoteList(self):
        """Fetch the IR remote list of the hub (blocking, run in executor)."""
        params = [("agt", self._agt)]
        tick, sign = _sign("GetRemoteList", params, self._usertoken, self._userid,
                           self._appkey, self._apptoken)
        send_values = {
            "id": 1,
            "method": "GetRemoteList",
            "params": {"agt": self._agt},
            "system": _system_block(tick, sign, self._userid, self._appkey),
        }
        response = _api_post(
            "https://api.ilifesmart.com/app/irapi.GetRemoteList", send_values
        )
        return response["message"]

    def _lifesmart_GetRemotes(self, ai):
        """Fetch one IR remote's codes (blocking, run in executor)."""
        params = [("agt", self._agt), ("ai", ai), ("needKeys", 2)]
        tick, sign = _sign("GetRemote", params, self._usertoken, self._userid,
                           self._appkey, self._apptoken)
        send_values = {
            "id": 1,
            "method": "GetRemote",
            "params": {"agt": self._agt, "ai": ai, "needKeys": 2},
            "system": _system_block(tick, sign, self._userid, self._appkey),
        }
        response = _api_post(
            "https://api.ilifesmart.com/app/irapi.GetRemote", send_values
        )
        return response["message"]["codes"]
