"""Constants for the PESC custom integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "pesc"

CONF_TOTP_SECRET = "totp_secret"
CONF_PROVIDER_CODES = "provider_codes"

DEFAULT_PROVIDER_CODES = ["PSK"]
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)
TOKEN_REFRESH_INTERVAL = timedelta(minutes=20)

PLATFORMS = ["binary_sensor", "sensor"]

SERVICE_SUBMIT_READINGS = "submit_readings"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ACCOUNT_ID = "account_id"
ATTR_METER = "meter"
ATTR_READINGS = "readings"

PESC_API_BASE_URL = "https://ikus.pesc.ru/api"
PESC_WEB_BASE_URL = "https://ikus.pesc.ru"
PESC_CUSTOMER_HEADER = "IKUS-SPB"
PESC_FRONTEND_VERSION = "3.17.0"
