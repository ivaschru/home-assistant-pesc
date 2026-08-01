"""Constants for the PESC custom integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "pesc"

CONF_ACCOUNT_TYPE = "account_type"
CONF_TOTP_SECRET = "totp_secret"
CONF_PROVIDER_CODES = "provider_codes"

ACCOUNT_TYPE_INDIVIDUAL = "individual"
ACCOUNT_TYPE_LEGAL = "legal"
ACCOUNT_TYPES = (ACCOUNT_TYPE_INDIVIDUAL, ACCOUNT_TYPE_LEGAL)

DEFAULT_PROVIDER_CODES = ["PSK"]
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)
TOKEN_REFRESH_INTERVAL = timedelta(minutes=20)

PLATFORMS = ["binary_sensor", "sensor"]

SERVICE_SUBMIT_READINGS = "submit_readings"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ACCOUNT_ID = "account_id"
ATTR_METER = "meter"
ATTR_READINGS = "readings"

PESC_INDIVIDUAL_API_BASE_URL = "https://ikus.pesc.ru/api"
PESC_INDIVIDUAL_WEB_BASE_URL = "https://ikus.pesc.ru"
PESC_INDIVIDUAL_CUSTOMER_HEADER = "IKUS-SPB"
PESC_LEGAL_API_BASE_URL = "https://lk.pesc.ru/api"
PESC_LEGAL_WEB_BASE_URL = "https://lk.pesc.ru"
PESC_LEGAL_CUSTOMER_HEADER = "spb"
PESC_FRONTEND_VERSION = "3.17.0"
