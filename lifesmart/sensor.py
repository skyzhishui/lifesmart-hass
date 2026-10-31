"""Support for lifesmart sensors."""
import logging

from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    UnitOfTemperature,
)

try:
    # HA 2026+: concentration units moved to UnitOfDensity / UnitOfRatio
    from homeassistant.const import UnitOfDensity, UnitOfRatio

    _UNIT_PPM = UnitOfRatio.PARTS_PER_MILLION
    _UNIT_MG_M3 = UnitOfDensity.MILLIGRAMS_PER_CUBIC_METER
except ImportError:
    # HA <= 2025: plain string constants
    from homeassistant.const import (
        CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER as _UNIT_MG_M3,
        CONCENTRATION_PARTS_PER_MILLION as _UNIT_PPM,
    )

from . import DOMAIN, LifeSmartEntity

_LOGGER = logging.getLogger(__name__)

GAS_SENSOR_TYPES = ["SL_SC_WA ",
"SL_SC_CH",
"SL_SC_CP",
"ELIQ_EM"]

OT_SENSOR_TYPES = ["SL_SC_MHW",
"SL_SC_BM",
"SL_SC_G",
"SL_SC_BG"]

EV_SENSOR_TYPES = ["SL_SC_THL",
"SL_SC_BE",
"SL_SC_CQ"]

# websocket updates for OT sensors only cover these idx
OT_UPDATE_IDX = ("Z", "V", "P3", "P4")


async def async_setup_entry(hass, entry, async_add_entities):
    """Perform the setup for LifeSmart devices."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("sensor", []):
        for idx in dev.get("data", {}):
            if dev["devtype"] in OT_SENSOR_TYPES and idx not in OT_UPDATE_IDX:
                # OT devices only report battery/illuminance/air readings here
                continue
            devices.append(LifeSmartSensor(dev, idx, dev["data"][idx], store["param"]))
    async_add_entities(devices)


class LifeSmartSensor(LifeSmartEntity, SensorEntity):
    """Representation of a LifeSmartSensor."""

    def __init__(self, dev, idx, val, param):
        """Initialize the LifeSmartSensor."""
        super().__init__(dev, idx, val, param)
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"] + "_" + idx).lower()
        )
        devtype = dev["devtype"]
        self._is_gas = devtype in GAS_SENSOR_TYPES
        if self._is_gas:
            self._attr_native_value = val.get("val")
            return
        if idx in ("T", "P1"):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif idx in ("H", "P2"):
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = PERCENTAGE
        elif idx == "Z":
            self._attr_device_class = SensorDeviceClass.ILLUMINANCE
            self._attr_native_unit_of_measurement = LIGHT_LUX
        elif idx == "V":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
        elif idx == "P3":
            self._attr_native_unit_of_measurement = _UNIT_PPM
        elif idx == "P4":
            self._attr_native_unit_of_measurement = _UNIT_MG_M3
        self._attr_native_value = val.get("v")

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        if self._is_gas:
            if msg.get("val", 0) > 0:
                self._attr_native_value = msg["val"]
            return
        self._attr_native_value = msg.get("v")
