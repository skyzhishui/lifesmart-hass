"""Support for LifeSmart binary sensors."""
import logging

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from . import DOMAIN, LifeSmartEntity

_LOGGER = logging.getLogger(__name__)


GUARD_SENSOR = ["SL_SC_G",
"SL_SC_BG"]
MOTION_SENSOR = ["SL_SC_MHW",
"SL_SC_BM",
"SL_SC_CM"]
SMOKE_SENSOR = ["SL_P_A"]


async def async_setup_entry(hass, entry, async_add_entities):
    """Perform the setup for lifesmart devices."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("binary_sensor", []):
        devices += [
            LifeSmartBinarySensor(dev, idx, dev["data"][idx], store["param"])
            for idx in dev.get("data", {})
            if idx in ("M", "G", "B", "AXS", "P1")
        ]
    async_add_entities(devices)


class LifeSmartBinarySensor(LifeSmartEntity, BinarySensorEntity):
    """Representation of LifeSmartBinarySensor."""

    def __init__(self, dev, idx, val, param):
        """Initialize the binary sensor."""
        super().__init__(dev, idx, val, param)
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"] + "_" + idx).lower()
        )
        devtype = dev["devtype"]
        if devtype in GUARD_SENSOR:
            self._attr_device_class = BinarySensorDeviceClass.DOOR
        elif devtype in MOTION_SENSOR:
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        else:
            self._attr_device_class = BinarySensorDeviceClass.SMOKE
        val_ = val["val"]
        if (val_ == 1 and self._attr_device_class != BinarySensorDeviceClass.DOOR) or (
            val_ == 0 and self._attr_device_class == BinarySensorDeviceClass.DOOR
        ):
            self._state = True
        else:
            self._state = False

    @property
    def is_on(self):
        """Return true if sensor is on."""
        return self._state

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        self._state = msg["val"] == 1
