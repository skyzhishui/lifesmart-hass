"""Support for LifeSmart covers."""
import logging

from homeassistant.components.cover import (
    ENTITY_ID_FORMAT,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
)

from . import DOMAIN, LifeSmartEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up lifesmart dooya cover devices."""
    store = hass.data[DOMAIN]
    devices = []
    for dev in store["platform_devices"].get("cover", []):
        idx = "P1"
        if idx not in dev.get("data", {}):
            continue
        devices.append(LifeSmartCover(dev, idx, dev["data"][idx], store["param"]))
    async_add_entities(devices)


class LifeSmartCover(LifeSmartEntity, CoverEntity):
    """LifeSmart cover devices."""

    def __init__(self, dev, idx, val, param):
        """Init LifeSmart cover device."""
        super().__init__(dev, idx, val, param)
        self._name = dev["name"]
        self.entity_id = ENTITY_ID_FORMAT.format(
            (dev["devtype"] + "_" + dev["agt"] + "_" + dev["me"]).lower()
        )
        self._pos = val["val"]
        self._attr_device_class = CoverDeviceClass.CURTAIN
        self._attr_is_opening = False
        self._attr_is_closing = False

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self._pos

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self._pos <= 0

    def close_cover(self, **kwargs):
        """Close the cover."""
        self._lifesmart_epset("0xCF", 0, "P2")

    def open_cover(self, **kwargs):
        """Open the cover."""
        self._lifesmart_epset("0xCF", 100, "P2")

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        self._lifesmart_epset("0xCE", 0x80, "P2")

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get(ATTR_POSITION)
        self._lifesmart_epset("0xCE", position, "P2")

    def handle_ws_update(self, msg):
        """Update state from a websocket io message."""
        nval = msg["val"]
        ntype = msg["type"]
        self._pos = nval & 0x7F
        if ntype % 2 == 0:
            self._attr_is_opening = False
            self._attr_is_closing = False
        else:
            self._attr_is_opening = bool(nval & 0x80)
            self._attr_is_closing = not self._attr_is_opening
