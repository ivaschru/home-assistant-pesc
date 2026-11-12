"""Recover our saved credentials after an integration-domain collision.

Another integration also uses ``pesc`` and can add its own version-2 token
fields to an existing entry. Those fields do not establish a working session
for this integration. Preserve our original credentials and unrelated settings;
never invent credentials or migrate an unrelated token-only entry silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    ACCOUNT_TYPE_INDIVIDUAL,
    ACCOUNT_TYPES,
    CONF_ACCOUNT_TYPE,
    CONF_TOTP_SECRET,
    CONFIG_ENTRY_VERSION,
)


class PescEntryMigrationError(ValueError):
    """Reject incompatible entry metadata without exposing its contents."""


def migrated_entry_data(data: Mapping[str, Any], version: int) -> dict[str, Any]:
    """Return a credential-preserving copy or refuse an unsupported migration.

    Version 1 is our original schema. Version 2 is recoverable only when the
    complete login/password/TOTP configuration survived the foreign migration.
    A future schema remains untouched, including its credentials, so installing
    an older component can never downgrade or discard an unknown format.
    """

    if version not in {1, 2, CONFIG_ENTRY_VERSION}:
        raise PescEntryMigrationError("Unsupported PESC config entry version")

    for key in ("username", "password", CONF_TOTP_SECRET):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PescEntryMigrationError("PESC login/password/TOTP are required")
    if data.get(CONF_ACCOUNT_TYPE, ACCOUNT_TYPE_INDIVIDUAL) not in ACCOUNT_TYPES:
        raise PescEntryMigrationError("Unsupported PESC account type")

    migrated = dict(data)
    if version == 2:
        # The foreign migrator injected auth={auth: None} and login_type=PHONE
        # even for our legal EMAIL/LOGIN cabinet. Remove only its known session
        # metadata; username/password/TOTP/provider options retain exact bytes.
        for key in ("auth", "login_type", "token"):
            migrated.pop(key, None)
    return migrated
