"""lifesmart switch @skyzhishui"""
import logging

from homeassistant.components.switch import (
    ENTITY_ID_FORMAT,
    SwitchEntity,
)

from . import DOMAIN, LifeSmartEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Find and return lifesmart switches."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("switch", []):
        devices += [
            LifeSmartSwitch(dev, idx, dev["data"][idx], store["param"])
            for idx in dev.get("data", {})
            if idx in ("L1", "L2", "L3", "P1", "P2", "P3")
        ]
    async_add_entities(devices)


class LifeSmartSwitch(LifeSmartEntity, SwitchEntity):
    """LifeSmart switch."""

    def __init__(self, dev, idx, val, param):
        """Initialize the switch."""
        super().__init__(dev, idx, val, param)
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"] + "_" + idx).lower()
        )
        self._state = val["type"] % 2 == 1

    @property
    def is_on(self):
        """Return true if device is on."""
        return self._state

    def turn_on(self, **kwargs):
        """Turn the device on."""
        if self._lifesmart_epset("0x81", 1, self._idx) == 0:
            self._state = True
            self.schedule_update_ha_state()

    def turn_off(self, **kwargs):
        """Turn the device off."""
        if self._lifesmart_epset("0x80", 0, self._idx) == 0:
            self._state = False
            self.schedule_update_ha_state()

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        self._state = msg["type"] % 2 == 1
