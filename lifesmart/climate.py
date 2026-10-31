"""Support for the LifeSmart climate devices."""
import logging
import time

from homeassistant.components.climate import (
    ENTITY_ID_FORMAT,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_WHOLE,
    UnitOfTemperature,
)

from . import DOMAIN, LifeSmartEntity, get_fan_mode

_LOGGER = logging.getLogger(__name__)
DEVICE_TYPE = "climate"

LIFESMART_STATE_LIST = [
    HVACMode.OFF,
    HVACMode.AUTO,
    HVACMode.FAN_ONLY,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
]

LIFESMART_STATE_LIST2 = [
    HVACMode.OFF,
    HVACMode.HEAT,
]

SPEED_LOW = "Speed_Low"
SPEED_MEDIUM = "Speed_Medium"
SPEED_HIGH = "Speed_High"

FAN_MODES = [SPEED_LOW, SPEED_MEDIUM, SPEED_HIGH]
GET_FAN_SPEED = {SPEED_LOW: 15, SPEED_MEDIUM: 45, SPEED_HIGH: 76}

AIR_TYPES = ["V_AIR_P"]

THER_TYPES = ["SL_CP_DN"]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up LifeSmart Climate devices."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("climate", []):
        if "T" not in dev.get("data", {}) and "P3" not in dev.get("data", {}):
            continue
        devices.append(LifeSmartClimateEntity(dev, "idx", "0", store["param"]))
    async_add_entities(devices)


class LifeSmartClimateEntity(LifeSmartEntity, ClimateEntity):
    """LifeSmart climate devices, include air conditioner, heater."""

    def __init__(self, dev, idx, val, param):
        """Init LifeSmart climate device."""
        super().__init__(dev, idx, val, param)
        self._name = dev["name"]
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"])
            .lower()
            .replace(":", "_")
            .replace("@", "_")
        )
        # one climate entity per device: ws messages carry various idx
        self._entity_key = (self._devtype, self._agt, self._me, None)
        cdata = dev.get("data", {})
        self._attr_precision = PRECISION_WHOLE
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_target_temperature_step = 1
        self._fanspeed = None
        if dev["devtype"] in AIR_TYPES:
            self._attr_hvac_modes = LIFESMART_STATE_LIST
            mode_val = cdata.get("MODE", {}).get("val", 0)
            if cdata.get("O", {}).get("type", 0) % 2 == 0:
                self._attr_hvac_mode = LIFESMART_STATE_LIST[0]
            else:
                self._attr_hvac_mode = LIFESMART_STATE_LIST[mode_val]
            self._attributes.setdefault("last_mode", LIFESMART_STATE_LIST[mode_val])
            self._attr_current_temperature = cdata.get("T", {}).get("v")
            self._attr_target_temperature = cdata.get("tT", {}).get("v")
            self._attr_min_temp = 10
            self._attr_max_temp = 35
            self._fanspeed = cdata.get("F", {}).get("val", 0)
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
            )
        else:
            self._attr_hvac_modes = LIFESMART_STATE_LIST2
            if cdata.get("P1", {}).get("type", 0) % 2 == 0:
                self._attr_hvac_mode = LIFESMART_STATE_LIST2[0]
            else:
                self._attr_hvac_mode = LIFESMART_STATE_LIST2[1]
            if cdata.get("P2", {}).get("type", 0) % 2 == 0:
                self._attributes.setdefault("Heating", "false")
            else:
                self._attributes.setdefault("Heating", "true")
            self._attr_current_temperature = cdata.get("P4", {}).get("val", 0) / 10
            self._attr_target_temperature = cdata.get("P3", {}).get("val", 0) / 10
            self._attr_min_temp = 5
            self._attr_max_temp = 35
            self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def fan_mode(self):
        """Return the fan setting."""
        if self._fanspeed is None:
            return None
        return get_fan_mode(self._fanspeed)

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        return FAN_MODES

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        if ATTR_TEMPERATURE not in kwargs:
            return
        new_temp = int(kwargs[ATTR_TEMPERATURE] * 10)
        _LOGGER.info("lifesmart set_temperature: %s", new_temp)
        idx = "tT" if self._devtype in AIR_TYPES else "P3"
        self._lifesmart_epset("0x88", new_temp, idx)

    def set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        self._lifesmart_epset("0xCE", GET_FAN_SPEED[fan_mode], "F")

    def set_hvac_mode(self, hvac_mode):
        """Set new target operation mode."""
        if self._devtype in AIR_TYPES:
            if hvac_mode == HVACMode.OFF:
                self._lifesmart_epset("0x80", 0, "O")
                return
            if self._attr_hvac_mode == HVACMode.OFF:
                if self._lifesmart_epset("0x81", 1, "O") == 0:
                    time.sleep(2)
                else:
                    return
            self._lifesmart_epset(
                "0xCE", LIFESMART_STATE_LIST.index(hvac_mode), "MODE"
            )
        else:
            if hvac_mode == HVACMode.OFF:
                self._lifesmart_epset("0x80", 0, "P1")
                time.sleep(1)
                self._lifesmart_epset("0x80", 0, "P2")
                return
            if self._lifesmart_epset("0x81", 1, "P1") == 0:
                time.sleep(2)

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        idx = msg.get("idx")
        ntype = msg.get("type")
        nval = msg.get("val")
        attrs = self._attributes
        if idx == "O":
            if ntype % 2 == 1:
                self._attr_hvac_mode = attrs.get("last_mode", self._attr_hvac_mode)
            else:
                self._attr_hvac_mode = HVACMode.OFF
        elif idx == "P1":
            if ntype % 2 == 1:
                self._attr_hvac_mode = HVACMode.HEAT
            else:
                self._attr_hvac_mode = HVACMode.OFF
        elif idx == "P2":
            attrs["Heating"] = "true" if ntype % 2 == 1 else "false"
        elif idx == "MODE":
            if ntype == 206:
                if self._attr_hvac_mode != HVACMode.OFF:
                    self._attr_hvac_mode = LIFESMART_STATE_LIST[nval]
                attrs["last_mode"] = LIFESMART_STATE_LIST[nval]
        elif idx == "F":
            if ntype == 206:
                self._fanspeed = nval
                attrs["fan_mode"] = get_fan_mode(nval)
        elif idx in ("tT", "P3"):
            if ntype == 136:
                self._attr_target_temperature = msg.get("v")
                attrs["temperature"] = msg.get("v")
        elif idx in ("T", "P4"):
            if ntype in (8, 9):
                self._attr_current_temperature = msg.get("v")
                attrs["current_temperature"] = msg.get("v")
