"""Config flow for login, password, and unattended PESC TOTP authentication."""

from __future__ import annotations

import hashlib
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    PescApiClient,
    PescApiError,
    PescAuthenticationError,
    PescConnectionError,
    normalize_login,
)
from .const import CONF_PROVIDER_CODES, CONF_TOTP_SECRET, DEFAULT_PROVIDER_CODES, DOMAIN
from .totp import InvalidTotpSecret, generate_totp


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the credential form while marking both secrets as passwords."""

    values = defaults or {}
    password_selector = TextSelector(
        TextSelectorConfig(
            type=TextSelectorType.PASSWORD, autocomplete="current-password"
        )
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME, default=values.get(CONF_USERNAME, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_PASSWORD): password_selector,
            vol.Required(CONF_TOTP_SECRET): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


class PescConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create or reauthenticate one PESC personal-account entry."""

    VERSION = 1

    async def _async_validate(self, user_input: dict[str, Any]) -> str:
        """Validate TOTP syntax and prove the full login against PESC."""

        # Parse and generate once before the network call to produce a useful
        # form error for malformed otpauth URIs without sending credentials.
        generate_totp(user_input[CONF_TOTP_SECRET])
        normalized_login, _ = normalize_login(user_input[CONF_USERNAME])
        client = PescApiClient(
            async_get_clientsession(self.hass),
            normalized_login,
            user_input[CONF_PASSWORD],
            user_input[CONF_TOTP_SECRET],
        )
        await client.async_validate_credentials()
        return normalized_login

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial setup from the Home Assistant UI."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                normalized_login = await self._async_validate(user_input)
            except InvalidTotpSecret:
                errors[CONF_TOTP_SECRET] = "invalid_totp"
            except PescAuthenticationError:
                errors["base"] = "invalid_auth"
            except PescConnectionError:
                errors["base"] = "cannot_connect"
            except PescApiError:
                errors["base"] = "unknown"
            else:
                unique_id = hashlib.sha256(normalized_login.encode()).hexdigest()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="PESC",
                    data={
                        CONF_USERNAME: normalized_login,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOTP_SECRET: user_input[CONF_TOTP_SECRET],
                        CONF_PROVIDER_CODES: DEFAULT_PROVIDER_CODES,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(user_input), errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after PESC rejects saved credentials."""

        entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm(
            {CONF_USERNAME: entry.data.get(CONF_USERNAME, "")}
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials and reload the existing entry."""

        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        defaults = {CONF_USERNAME: entry.data.get(CONF_USERNAME, "")}
        if user_input is not None and CONF_PASSWORD in user_input:
            try:
                normalized_login = await self._async_validate(user_input)
            except InvalidTotpSecret:
                errors[CONF_TOTP_SECRET] = "invalid_totp"
            except PescAuthenticationError:
                errors["base"] = "invalid_auth"
            except PescConnectionError:
                errors["base"] = "cannot_connect"
            except PescApiError:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: normalized_login,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOTP_SECRET: user_input[CONF_TOTP_SECRET],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_user_schema(defaults),
            errors=errors,
        )
