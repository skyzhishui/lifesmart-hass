"""Config flow to add the lifesmart integration from the web UI."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from . import (
    CONF_LIFESMART_APPTOKEN,
    CONF_LIFESMART_APPKEY,
    CONF_LIFESMART_FALLBACK,
    CONF_LIFESMART_LOCAL_PASSWORD,
    CONF_LIFESMART_LOCAL_USERNAME,
    CONF_LIFESMART_MODE,
    MODE_OFFLINE,
    MODE_ONLINE,
    _cloud_login,
    lifesmart_EpGetAll,
)
from .local import probe

_LOGGER = logging.getLogger(__name__)


class CannotConnect(HomeAssistantError):
    """Station/cloud unreachable."""


class InvalidAuth(HomeAssistantError):
    """Wrong credentials."""


def _validate_cloud(data: dict[str, Any]) -> str:
    """Cloud login + device fetch; returns the account agt (executor)."""
    param = {
        "appkey": data[CONF_LIFESMART_APPKEY],
        "apptoken": data[CONF_LIFESMART_APPTOKEN],
        "username": data[CONF_USERNAME],
        "passwd": data[CONF_PASSWORD],
    }
    _cloud_login(param)
    devices = lifesmart_EpGetAll(
        param["appkey"], param["apptoken"], param["usertoken"], param["userid"]
    )
    if not devices:
        raise CannotConnect("EpGetAll failed")
    return devices[0]["agt"].replace("_", "")


def _validate_local(data: dict[str, Any]) -> str:
    """Station login + device fetch; returns the station agt (executor)."""
    node, _node_agt, _eps = probe(
        data[CONF_HOST],
        data[CONF_LIFESMART_LOCAL_USERNAME],
        data[CONF_LIFESMART_LOCAL_PASSWORD],
    )
    return node


def _map_error(err: Exception) -> str:
    """Map a validation exception to a flow error key."""
    if isinstance(err, PermissionError):
        return "invalid_auth"
    if isinstance(err, (TimeoutError, ConnectionError, OSError)):
        return "cannot_connect"
    if isinstance(err, (ValueError, KeyError)):
        return "invalid_auth"
    return "unknown"


class LifeSmartConfigFlow(config_entries.ConfigFlow, domain="lifesmart"):
    """Handle the lifesmart config flow (online/offline)."""

    VERSION = 1

    def __init__(self) -> None:
        """Init the flow."""
        self._mode = MODE_ONLINE

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: pick online (cloud) or offline (LAN) mode."""
        errors = {}
        if user_input is not None:
            self._mode = user_input[CONF_LIFESMART_MODE]
            if self._mode == MODE_OFFLINE:
                return await self.async_step_offline()
            return await self.async_step_online()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LIFESMART_MODE, default=MODE_ONLINE
                    ): vol.In([MODE_ONLINE, MODE_OFFLINE]),
                }
            ),
            errors=errors,
        )

    async def async_step_online(self, user_input=None) -> FlowResult:
        """Step 2 (online): cloud credentials."""
        errors = {}
        if user_input is not None:
            try:
                agt = await self.hass.async_add_executor_job(
                    _validate_cloud, user_input
                )
            except (HomeAssistantError, OSError, ValueError, KeyError) as err:
                errors["base"] = _map_error(err)
            else:
                await self.async_set_unique_id(agt)
                self._abort_if_unique_id_mismatches()
                return self.async_create_entry(
                    title=f"LifeSmart {agt}",
                    data={**user_input, CONF_LIFESMART_MODE: MODE_ONLINE},
                )
        return self.async_show_form(
            step_id="online",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIFESMART_APPKEY): str,
                    vol.Required(CONF_LIFESMART_APPTOKEN): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_offline(self, user_input=None) -> FlowResult:
        """Step 2 (offline): station address + local admin credentials."""
        errors = {}
        if user_input is not None:
            if user_input[CONF_LIFESMART_FALLBACK] and (
                    not user_input.get(CONF_LIFESMART_APPKEY)
                    or not user_input.get(CONF_LIFESMART_APPTOKEN)
                    or not user_input.get(CONF_USERNAME)
                    or not user_input.get(CONF_PASSWORD)):
                # fallback logs into the cloud with username/password
                errors["base"] = "fallback_needs_cloud"
            else:
                try:
                    agt = await self.hass.async_add_executor_job(
                        _validate_local, user_input
                    )
                except (HomeAssistantError, OSError, ValueError, KeyError,
                        TimeoutError) as err:
                    errors["base"] = _map_error(err)
                else:
                    await self.async_set_unique_id(agt)
                    self._abort_if_unique_id_mismatches()
                    return self.async_create_entry(
                        title=f"LifeSmart {agt}",
                        data={**user_input, CONF_LIFESMART_MODE: MODE_OFFLINE},
                    )
        return self.async_show_form(
            step_id="offline",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(
                        CONF_LIFESMART_LOCAL_USERNAME, default="admin"
                    ): str,
                    vol.Required(CONF_LIFESMART_LOCAL_PASSWORD): str,
                    vol.Required(CONF_LIFESMART_FALLBACK, default=False): bool,
                    vol.Optional(CONF_LIFESMART_APPKEY): str,
                    vol.Optional(CONF_LIFESMART_APPTOKEN): str,
                    vol.Optional(CONF_USERNAME): str,
                    vol.Optional(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, data: dict[str, Any]) -> FlowResult:
        """Import the YAML config as a config entry (no validation here)."""
        mode = data.get(CONF_LIFESMART_MODE, MODE_ONLINE)
        return self.async_create_entry(
            title=f"LifeSmart ({mode})", data=data
        )
