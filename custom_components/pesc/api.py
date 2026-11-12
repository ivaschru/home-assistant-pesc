"""Asynchronous client for the private API used by the PESC web cabinet.

PESC does not publish a supported public API for this workflow.  This client
therefore mirrors only the narrow requests used by the official web frontend.
Every write is validated against a freshly discovered meter before it is sent.
The module never logs request bodies, tokens, passwords, or TOTP material.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    ACCOUNT_TYPE_INDIVIDUAL,
    ACCOUNT_TYPE_LEGAL,
    DEFAULT_PROVIDER_CODES,
    PESC_FRONTEND_VERSION,
    PESC_INDIVIDUAL_API_BASE_URL,
    PESC_INDIVIDUAL_CUSTOMER_HEADER,
    PESC_LEGAL_API_BASE_URL,
    PESC_LEGAL_CUSTOMER_HEADER,
)
from .models import (
    PescAccount,
    PescMeter,
    PescMeterScale,
    PescReadingPeriod,
    PescSnapshot,
    PescSubmissionResult,
)
from .totp import generate_totp


class PescApiError(Exception):
    """Base error raised for a failed PESC request."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Keep the safe provider reason separately from transport credentials.

        Automation callers need the provider's exact rejection reason.  Retain
        only its message, code and status, never the complete response, request
        headers or authentication payload.  Existing exception-based callers
        still see the same message through ``str(error)``.
        """

        super().__init__(message)
        self.code = code
        self.http_status = http_status


class PescHighConsumptionWarning(PescApiError):
    """The exact legal register advisory requiring explicit confirmation."""


class PescAuthenticationError(PescApiError):
    """Raised when PESC credentials or the TOTP challenge are rejected."""


class PescConnectionError(PescApiError):
    """Raised when the PESC endpoint cannot be reached."""


class PescValidationError(PescApiError):
    """Raised before an unsafe or malformed meter submission is transmitted."""


def normalize_login(
    value: str, account_type: str = ACCOUNT_TYPE_INDIVIDUAL
) -> tuple[str, str]:
    """Normalize a login according to the selected PESC cabinet."""

    login = value.strip()
    if "@" in login:
        return login.lower(), "EMAIL"

    # The legal cabinet advertises EMAIL and LOGIN authentication.  Unlike the
    # individual cabinet it does not normalize arbitrary logins as Russian
    # phone numbers, because a contract login may be an organization username.
    if account_type == ACCOUNT_TYPE_LEGAL:
        if not login:
            raise PescAuthenticationError("The legal-account login is empty")
        return login, "LOGIN"

    digits = re.sub(r"\D", "", login)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise PescAuthenticationError("The phone login must contain 11 Russian digits")
    return f"+{digits}", "PHONE"


def _parse_date(value: Any) -> date | None:
    """Parse the date formats currently returned by the PESC API."""

    if not isinstance(value, str) or not value:
        return None
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    # The legal cabinet returns the preceding reading month in Russian, for
    # example ``июнь 2026``.  Store the first day as a stable month marker.
    russian_months = {
        "январь": 1,
        "февраль": 2,
        "март": 3,
        "апрель": 4,
        "май": 5,
        "июнь": 6,
        "июль": 7,
        "август": 8,
        "сентябрь": 9,
        "октябрь": 10,
        "ноябрь": 11,
        "декабрь": 12,
    }
    match = re.fullmatch(
        r"([\u0430-\u044f\u0451]+)\s+(\d{4})", value.strip().casefold()
    )
    if match and match.group(1) in russian_months:
        return date(int(match.group(2)), russian_months[match.group(1)], 1)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an upstream bill timestamp without guessing a timezone."""

    if not isinstance(value, str) or not value:
        return None
    for date_format in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    return None


class PescApiClient:
    """Authenticated, retrying client for one selected PESC cabinet type."""

    def __init__(
        self,
        session: ClientSession,
        login: str,
        password: str,
        totp_secret: str | None,
        *,
        account_type: str = ACCOUNT_TYPE_INDIVIDUAL,
        provider_codes: list[str] | None = None,
        verification_token: str | None = None,
    ) -> None:
        self._session = session
        if account_type not in {ACCOUNT_TYPE_INDIVIDUAL, ACCOUNT_TYPE_LEGAL}:
            raise ValueError(f"Unsupported PESC account type: {account_type}")
        self._account_type = account_type
        self._login, self._login_type = normalize_login(login, account_type)
        self._password = password
        self._totp_secret = totp_secret
        self._verification_token = verification_token
        self._provider_codes = {
            code.upper() for code in (provider_codes or DEFAULT_PROVIDER_CODES)
        }
        self._tokens: dict[str, str] | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def account_type(self) -> str:
        """Return the cabinet type selected for this client."""

        return self._account_type

    @property
    def _api_base_url(self) -> str:
        """Use the host belonging to the selected cabinet."""

        if self._account_type == ACCOUNT_TYPE_LEGAL:
            return PESC_LEGAL_API_BASE_URL
        return PESC_INDIVIDUAL_API_BASE_URL

    @property
    def _customer_header(self) -> str:
        """Return the frontend customer marker expected by that host."""

        if self._account_type == ACCOUNT_TYPE_LEGAL:
            return PESC_LEGAL_CUSTOMER_HEADER
        return PESC_INDIVIDUAL_CUSTOMER_HEADER

    @staticmethod
    def _jwt_ttl(token: str | None) -> float:
        """Read a JWT expiry locally; signature verification remains server-side."""

        if not token:
            return 0
        try:
            payload = token.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return float(claims["exp"]) - time.time()
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return 0

    @staticmethod
    async def _response_json(response: ClientResponse) -> Any:
        """Decode JSON regardless of the occasionally incorrect content type."""

        text = await response.text()
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise PescApiError("PESC returned an invalid JSON response") from exc

    @staticmethod
    def _error_message(payload: Any, status: int) -> str:
        """Extract a user-safe upstream message without serializing whole payloads."""

        if isinstance(payload, dict):
            message = payload.get("message")
            code = payload.get("code")
            if isinstance(message, str) and message.strip():
                return message.strip()
            if code not in (None, ""):
                return f"PESC API error {code}"
        return f"PESC API returned HTTP {status}"

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        """Build the minimal header set used by the official web frontend."""

        headers = {
            "Content-Type": "application/json",
            "VERSION": PESC_FRONTEND_VERSION,
            "customer": self._customer_header,
        }
        if authenticated and self._tokens and self._tokens.get("auth"):
            headers["Authorization"] = f"Bearer {self._tokens['auth']}"
        return headers

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        authenticated: bool = True,
        allowed_statuses: set[int] | None = None,
    ) -> tuple[int, Any]:
        """Execute one request and return only a decoded, in-memory payload."""

        allowed = allowed_statuses or {200, 201, 204}
        try:
            async with self._session.request(
                method,
                f"{self._api_base_url}/{path.lstrip('/')}",
                json=payload,
                headers=self._headers(authenticated=authenticated),
            ) as response:
                status = response.status
                data = await self._response_json(response)
        except (ClientError, TimeoutError) as exc:
            raise PescConnectionError("Unable to connect to PESC") from exc

        if status not in allowed:
            error = self._error_message(data, status)
            code = data.get("code") if isinstance(data, dict) else None
            metadata = {
                "code": str(code) if code not in (None, "") else None,
                "http_status": status,
            }
            if status in {401, 403, 424}:
                raise PescAuthenticationError(error, **metadata)
            raise PescApiError(error, **metadata)
        return status, data

    async def async_authenticate(self) -> None:
        """Create a session using login, password, and an automatic TOTP code."""

        async with self._auth_lock:
            login_headers = self._headers(authenticated=False)
            login_headers.update({"Captcha": "none", "withTotp": "true"})
            if self._verification_token:
                # This is the same trusted-device token used by the official
                # frontend.  It avoids storing a permanent TOTP seed when a
                # config flow has completed a one-time verification instead.
                login_headers["Auth-Verification"] = self._verification_token
            try:
                async with self._session.post(
                    f"{self._api_base_url}/v8/users/auth",
                    json={
                        "login": self._login,
                        "password": self._password,
                        "type": self._login_type,
                    },
                    headers=login_headers,
                ) as response:
                    status = response.status
                    payload = await self._response_json(response)
            except (ClientError, TimeoutError) as exc:
                raise PescConnectionError("Unable to connect to PESC") from exc

            if status == 200 and isinstance(payload, dict):
                self._tokens = self._validate_tokens(payload)
                if self._tokens.get("verified"):
                    self._verification_token = self._tokens["verified"]
                return

            if status != 424 or not isinstance(payload, dict):
                raise PescAuthenticationError(self._error_message(payload, status))

            transaction_id = payload.get("transactionId")
            challenge_types = payload.get("types", [])
            if not isinstance(transaction_id, str) or "TOTP" not in challenge_types:
                raise PescAuthenticationError("PESC did not offer a TOTP challenge")
            if not self._totp_secret:
                raise PescAuthenticationError(
                    "PESC requires a permanent TOTP secret for unattended login"
                )

            _, verified_payload = await self._raw_request(
                "POST",
                f"v1/dfa/{quote(transaction_id, safe='')}/totp/verify",
                payload={"code": generate_totp(self._totp_secret)},
                authenticated=False,
            )
            if not isinstance(verified_payload, dict):
                raise PescAuthenticationError(
                    "PESC returned invalid authentication tokens"
                )
            self._tokens = self._validate_tokens(verified_payload)
            if self._tokens.get("verified"):
                self._verification_token = self._tokens["verified"]

    @staticmethod
    def _validate_tokens(payload: Mapping[str, Any]) -> dict[str, str]:
        """Keep only the three documented session tokens and validate their types."""

        tokens = {
            key: value
            for key in ("auth", "access", "verified")
            if isinstance((value := payload.get(key)), str) and value
        }
        if "auth" not in tokens or "access" not in tokens:
            raise PescAuthenticationError(
                "PESC returned incomplete authentication tokens"
            )
        return tokens

    async def async_refresh_tokens(self) -> None:
        """Refresh short-lived tokens without reusing or exposing the TOTP secret."""

        async with self._auth_lock:
            if not self._tokens:
                # Release the lock before the full login path acquires it again.
                needs_login = True
            else:
                needs_login = False

            if not needs_login:
                try:
                    _, payload = await self._raw_request(
                        "PUT", "v6/users/auth", payload=self._tokens
                    )
                except PescAuthenticationError:
                    self._tokens = None
                    needs_login = True
                else:
                    if not isinstance(payload, dict):
                        raise PescAuthenticationError(
                            "PESC returned invalid refresh tokens"
                        )
                    refreshed = self._validate_tokens(payload)
                    # The long-lived verification token is not returned by refresh.
                    if self._tokens.get("verified"):
                        refreshed["verified"] = self._tokens["verified"]
                    self._tokens = refreshed

        if needs_login:
            await self.async_authenticate()

    async def _ensure_authenticated(self) -> None:
        """Guarantee a token with enough lifetime for the next API operation."""

        auth_token = self._tokens.get("auth") if self._tokens else None
        if self._jwt_ttl(auth_token) > 60:
            return

        access_token = self._tokens.get("access") if self._tokens else None
        if self._tokens and self._jwt_ttl(access_token) > 60:
            await self.async_refresh_tokens()
            return
        await self.async_authenticate()

    async def _request(self, method: str, path: str, *, payload: Any = None) -> Any:
        """Execute one authenticated request with a single re-auth retry."""

        await self._ensure_authenticated()
        try:
            _, data = await self._raw_request(method, path, payload=payload)
            return data
        except PescAuthenticationError:
            self._tokens = None
            await self.async_authenticate()
            _, data = await self._raw_request(method, path, payload=payload)
            return data

    async def async_validate_credentials(self) -> None:
        """Validate credentials and basic account access for config flow setup."""

        await self.async_authenticate()
        account_path = (
            "v8/accounts"
            if self._account_type == ACCOUNT_TYPE_LEGAL
            else "v8/accounts/light"
        )
        accounts = await self._request("GET", account_path)
        if not isinstance(accounts, list):
            raise PescApiError("PESC returned an invalid account list")

    async def _async_account_snapshot(
        self, raw_account: Mapping[str, Any]
    ) -> PescAccount:
        """Fetch all read-only account data concurrently for one object."""

        account_id = int(raw_account["id"])
        (
            period_data,
            state_data,
            meters_data,
            balance_data,
            bill_data,
        ) = await asyncio.gather(
            self._request("GET", f"v6/accounts/{account_id}/reading/period"),
            self._request("GET", f"v6/accounts/{account_id}/reading/state"),
            self._request("GET", f"v6/accounts/{account_id}/meters/info"),
            self._request(
                "GET", f"v7/accounts/{account_id}/payments/at/current/amount/discretion"
            ),
            self._request("GET", f"v8/accounts/{account_id}/payments/bills/current"),
        )

        return PescAccount(
            account_id=account_id,
            alias=str(raw_account.get("alias") or f"Account {account_id}"),
            account_number=self._account_number(raw_account),
            provider_code=self._provider_code(raw_account),
            auto_payment_on=bool(raw_account.get("autoPaymentOn")),
            delivery=(
                str(raw_account["delivery"])
                if raw_account.get("delivery") is not None
                else None
            ),
            reading_state=str(state_data) if state_data is not None else None,
            reading_period=self._reading_period(period_data),
            **self._balance_fields(balance_data),
            current_bill_amount=(
                float(bill_data["amount"])
                if isinstance(bill_data, dict) and bill_data.get("amount") is not None
                else None
            ),
            current_bill_timestamp=(
                _parse_datetime(bill_data.get("timestamp"))
                if isinstance(bill_data, dict)
                else None
            ),
            meters=self._meters(meters_data),
            account_type=ACCOUNT_TYPE_INDIVIDUAL,
        )

    async def _async_legal_account_snapshot(
        self, raw_account: Mapping[str, Any]
    ) -> PescAccount:
        """Fetch a legal contract, its energy objects, and current meters."""

        account_id = int(raw_account["id"])
        period_data, energy_objects = await asyncio.gather(
            self._request("GET", f"v6/accounts/{account_id}/reading/period"),
            self._request(
                "GET", f"v6/users/current/accounts/{account_id}/energy-objects"
            ),
        )
        if not isinstance(energy_objects, list):
            raise PescApiError("PESC returned an invalid legal energy-object list")

        usable_objects = [
            item
            for item in energy_objects
            if isinstance(item, dict) and item.get("id") is not None
        ]
        meter_payloads = await asyncio.gather(
            *(
                self._request(
                    "GET",
                    (
                        f"v6/accounts/{account_id}/energy-objects/"
                        f"{int(item['id'])}/indications/actual"
                    ),
                )
                for item in usable_objects
            )
        )
        meters = tuple(
            meter
            for payload in meter_payloads
            for meter in self._meters(payload, legal=True)
        )

        # PENDING means that the current legal-account period is still waiting
        # for readings.  Once every scale has a current reading, keep the same
        # REGISTERED state used by individual accounts so automations can share
        # one duplicate guard across both cabinet types.
        active_scales = [scale for meter in meters for scale in meter.scales]
        if active_scales and all(
            scale.current_reading is not None for scale in active_scales
        ):
            reading_state = "REGISTERED"
        elif any(not bool(item.get("forbidden")) for item in usable_objects):
            reading_state = "AVAILABLE"
        else:
            reading_state = "UNAVAILABLE"

        addresses = [
            str(item["eoAddress"]) for item in usable_objects if item.get("eoAddress")
        ]
        account_number = self._account_number(raw_account)
        alias = (
            addresses[0]
            if len(addresses) == 1
            else str(raw_account.get("alias") or account_number or account_id)
        )
        return PescAccount(
            account_id=account_id,
            alias=alias,
            account_number=account_number,
            provider_code="PSK",
            auto_payment_on=bool(raw_account.get("autoPaymentOn")),
            delivery=(
                str(raw_account["delivery"])
                if raw_account.get("delivery") is not None
                else None
            ),
            reading_state=reading_state,
            reading_period=self._reading_period(period_data),
            balance=None,
            amount_due=None,
            overpayment=None,
            current_bill_amount=None,
            current_bill_timestamp=None,
            meters=meters,
            account_type=ACCOUNT_TYPE_LEGAL,
        )

    @staticmethod
    def _provider_code(raw_account: Mapping[str, Any]) -> str:
        service = raw_account.get("service")
        if not isinstance(service, dict):
            return ""
        return str(service.get("providerCode") or "").upper()

    @staticmethod
    def _account_number(raw_account: Mapping[str, Any]) -> str | None:
        tenancy = raw_account.get("tenancy")
        if not isinstance(tenancy, dict) or tenancy.get("register") is None:
            return None
        return str(tenancy["register"])

    @staticmethod
    def _reading_period(payload: Any) -> PescReadingPeriod | None:
        if not isinstance(payload, dict):
            return None
        acceptance = payload.get("acceptanceParameters")
        acceptance = acceptance if isinstance(acceptance, dict) else {}
        interval = acceptance.get("interval")
        interval = interval if isinstance(interval, dict) else {}
        return PescReadingPeriod(
            name=str(acceptance["name"]) if acceptance.get("name") else None,
            date_from=_parse_date(interval.get("dateFrom")),
            date_to=_parse_date(interval.get("dateTo")),
            deadline_day=(
                int(acceptance["deadLine"])
                if acceptance.get("deadLine") is not None
                else None
            ),
            forbidden=bool(payload.get("forbidden")),
            message=str(payload["message"]) if payload.get("message") else None,
        )

    @staticmethod
    def _meters(payload: Any, *, legal: bool = False) -> tuple[PescMeter, ...]:
        if not isinstance(payload, list):
            return ()
        meters: list[PescMeter] = []
        for raw_meter in payload:
            if not isinstance(raw_meter, dict):
                continue
            raw_id = raw_meter.get("id")
            if not isinstance(raw_id, dict) or raw_id.get("registration") is None:
                continue
            scales: list[PescMeterScale] = []
            for raw_scale in raw_meter.get("indications", []):
                if (
                    not isinstance(raw_scale, dict)
                    or raw_scale.get("meterScaleId") is None
                ):
                    continue
                scales.append(
                    PescMeterScale(
                        scale_id=int(raw_scale["meterScaleId"]),
                        name=str(
                            (
                                raw_scale.get("timeOfUse")
                                if legal
                                else raw_scale.get("scaleName")
                            )
                            or raw_scale.get("scaleName")
                            or raw_scale["meterScaleId"]
                        ),
                        previous_reading=(
                            float(raw_scale["previousReading"])
                            if raw_scale.get("previousReading") is not None
                            else None
                        ),
                        previous_reading_date=_parse_date(
                            raw_scale.get("previousReadingDate")
                        ),
                        unit=(
                            str(raw_scale.get("unit") or raw_scale.get("unitOfMeasure"))
                            if raw_scale.get("unit") is not None
                            or raw_scale.get("unitOfMeasure") is not None
                            else None
                        ),
                        register_id=(
                            str(raw_scale["registerId"])
                            if raw_scale.get("registerId") is not None
                            else None
                        ),
                        indication_id=(
                            int(raw_scale["indicationId"])
                            if raw_scale.get("indicationId") is not None
                            else None
                        ),
                        upstream_name=(
                            str(raw_scale["scaleName"])
                            if raw_scale.get("scaleName") is not None
                            else None
                        ),
                        time_of_use=(
                            str(raw_scale["timeOfUse"])
                            if raw_scale.get("timeOfUse") is not None
                            else None
                        ),
                        current_reading=(
                            float(raw_scale["registerReading"])
                            if raw_scale.get("registerReading") is not None
                            else None
                        ),
                    )
                )
            meters.append(
                PescMeter(
                    registration=str(raw_id["registration"]),
                    provider_id=int(
                        raw_id.get("provider") or raw_id.get("providerId") or 0
                    ),
                    serial=str(raw_meter.get("serial") or raw_id["registration"]),
                    name=str(
                        raw_meter.get("name") or raw_meter.get("serial") or "Meter"
                    ),
                    status=(
                        str(raw_meter["status"])
                        if raw_meter.get("status") is not None
                        else None
                    ),
                    digits_left=(
                        int(raw_meter["numberOfDigitsLeft"])
                        if raw_meter.get("numberOfDigitsLeft") is not None
                        else None
                    ),
                    digits_right=(
                        int(raw_meter["numberOfDigitsRight"])
                        if raw_meter.get("numberOfDigitsRight") is not None
                        else None
                    ),
                    scales=tuple(scales),
                )
            )
        return tuple(meters)

    @staticmethod
    def _balance_fields(payload: Any) -> dict[str, float | None]:
        amount_due = 0.0
        overpayment = 0.0
        found = False
        if isinstance(payload, list):
            for service in payload:
                if not isinstance(service, dict):
                    continue
                for section_name in ("charge", "fine"):
                    section = service.get(section_name)
                    balance = (
                        section.get("balance") if isinstance(section, dict) else None
                    )
                    if not isinstance(balance, dict) or balance.get("value") is None:
                        continue
                    found = True
                    value = float(balance["value"])
                    balance_type = int(balance.get("type") or 0)
                    if balance_type == 2:
                        amount_due += value
                    elif balance_type == 3:
                        overpayment += value
        return {
            "balance": overpayment - amount_due if found else None,
            "amount_due": amount_due if found else None,
            "overpayment": overpayment if found else None,
        }

    async def async_get_snapshot(self) -> PescSnapshot:
        """Discover configured provider accounts and fetch their current data."""

        account_path = (
            "v8/accounts"
            if self._account_type == ACCOUNT_TYPE_LEGAL
            else "v8/accounts/light"
        )
        raw_accounts = await self._request("GET", account_path)
        if not isinstance(raw_accounts, list):
            raise PescApiError("PESC returned an invalid account list")

        if self._account_type == ACCOUNT_TYPE_LEGAL:
            # Legal contracts currently return ``providerCode: null`` even for
            # PESC electricity.  The selected cabinet itself is the provider
            # boundary, so filtering on that field would hide every contract.
            selected_accounts = [
                account for account in raw_accounts if isinstance(account, dict)
            ]
            snapshot_factory = self._async_legal_account_snapshot
        else:
            selected_accounts = [
                account
                for account in raw_accounts
                if isinstance(account, dict)
                and self._provider_code(account) in self._provider_codes
            ]
            snapshot_factory = self._async_account_snapshot
        accounts = await asyncio.gather(
            *(snapshot_factory(account) for account in selected_accounts)
        )
        return PescSnapshot(
            accounts={account.account_id: account for account in accounts}
        )

    @staticmethod
    def _find_meter(account: PescAccount, meter_reference: str | None) -> PescMeter:
        if meter_reference is None:
            if len(account.meters) != 1:
                raise PescValidationError(
                    "Multiple meters found; specify the serial or registration"
                )
            return account.meters[0]

        reference = meter_reference.strip().casefold()
        matches = [
            meter
            for meter in account.meters
            if meter.serial.casefold() == reference
            or meter.registration.casefold() == reference
        ]
        if len(matches) != 1:
            raise PescValidationError("The requested meter was not found uniquely")
        return matches[0]

    @staticmethod
    def _scale_values(
        meter: PescMeter, readings: Mapping[str, Any]
    ) -> tuple[list[dict[str, float | int]], dict[str, float]]:
        """Validate a complete set of readings and build the upstream payload."""

        normalized = {
            str(key).strip().casefold(): value for key, value in readings.items()
        }
        expected_keys = {str(scale.scale_id).casefold() for scale in meter.scales}
        for scale in meter.scales:
            expected_keys.add(scale.name.strip().casefold())
            if scale.upstream_name:
                expected_keys.add(scale.upstream_name.strip().casefold())
            if scale.time_of_use:
                expected_keys.add(scale.time_of_use.strip().casefold())
        unknown_keys = set(normalized) - expected_keys
        if unknown_keys:
            raise PescValidationError("One or more reading scale names are unknown")

        payload: list[dict[str, float | int]] = []
        result: dict[str, float] = {}
        for scale in meter.scales:
            name_key = scale.name.strip().casefold()
            id_key = str(scale.scale_id).casefold()
            if name_key in normalized:
                raw_value = normalized[name_key]
            elif id_key in normalized:
                raw_value = normalized[id_key]
            elif (
                scale.upstream_name
                and scale.upstream_name.strip().casefold() in normalized
            ):
                raw_value = normalized[scale.upstream_name.strip().casefold()]
            elif (
                scale.time_of_use and scale.time_of_use.strip().casefold() in normalized
            ):
                raw_value = normalized[scale.time_of_use.strip().casefold()]
            else:
                raise PescValidationError("A value is required for every meter scale")

            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise PescValidationError("Every reading must be numeric") from exc
            if not math.isfinite(value) or value < 0:
                raise PescValidationError(
                    "Every reading must be a finite positive value"
                )
            if scale.previous_reading is not None and value < scale.previous_reading:
                raise PescValidationError(
                    f"Reading for {scale.name} is lower than the previous PESC reading"
                )

            digits_right = meter.digits_right or 0
            if round(value, digits_right) != value:
                raise PescValidationError(
                    f"Reading for {scale.name} exceeds the meter precision"
                )
            if (
                meter.digits_left is not None
                and len(str(int(value))) > meter.digits_left
            ):
                raise PescValidationError(
                    f"Reading for {scale.name} exceeds the meter capacity"
                )

            upstream_value: float | int = int(value) if digits_right == 0 else value
            payload.append({"scaleId": scale.scale_id, "value": upstream_value})
            result[scale.name] = value

        return payload, result

    @staticmethod
    def _reading_path_value(value: float, digits_right: int | None) -> str:
        """Format a validated reading for the legal validation URL."""

        precision = digits_right or 0
        rendered = f"{value:.{precision}f}"
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered

    async def _async_submit_legal_readings(
        self,
        account: PescAccount,
        meter: PescMeter,
        normalized_readings: Mapping[str, float],
        *,
        confirm_high_consumption: bool = False,
    ) -> None:
        """Validate every legal register and send the official web payload."""

        scale_payloads: list[dict[str, Any]] = []
        for scale in meter.scales:
            if scale.register_id is None or scale.indication_id is None:
                raise PescValidationError(
                    "PESC omitted legal-account register metadata"
                )
            value = normalized_readings[scale.name]
            value_path = self._reading_path_value(value, meter.digits_right)

            # The official cabinet validates each register before the final
            # contract write.  Repeating that call here preserves its abnormal-
            # consumption protection instead of relying on local monotonicity.
            try:
                await self._request(
                    "POST",
                    (
                        f"v7/accounts/{account.account_id}/meters/"
                        f"{quote(meter.registration, safe='')}/scales/"
                        f"{quote(scale.register_id, safe='')}/reading/"
                        f"{quote(value_path, safe='')}/validate"
                    ),
                    payload={},
                )
            except PescApiError as error:
                # Only this known advisory from per-register validation can
                # be confirmed.  A final-write error with the same HTTP status,
                # other warnings, authentication and transport errors still
                # fail normally.  Every other register is validated as usual.
                if error.http_status != 416 or error.code != "LKK_SERVICE_054":
                    raise
                if not confirm_high_consumption:
                    raise PescHighConsumptionWarning(
                        str(error), code=error.code, http_status=error.http_status
                    ) from error
            scale_payloads.append(
                {
                    "meterScaleId": scale.scale_id,
                    "registerId": scale.register_id,
                    "indicationId": scale.indication_id,
                    "scaleName": scale.upstream_name or scale.name,
                    "timeOfUse": scale.time_of_use or scale.name,
                    "registerReading": value,
                    "previousReading": scale.previous_reading,
                    "delta": (
                        value - scale.previous_reading
                        if scale.previous_reading is not None
                        else 0
                    ),
                    "indicationDate": None,
                }
            )

        response = await self._request(
            "POST",
            f"v7/accounts/{account.account_id}/indications",
            payload={
                "accountId": account.account_id,
                "meterId": {
                    "providerId": meter.provider_id,
                    "registration": meter.registration,
                },
                "scales": scale_payloads,
                "serial": meter.serial,
                "uuid": str(uuid.uuid4()),
            },
        )
        # Legal accounts normally return a localized success message.  Empty
        # bodies/objects and ``true`` are also accepted by deployed API builds;
        # HTTP status validation has already rejected unsuccessful responses.
        if response is False or (
            isinstance(response, dict) and response.get("success") is False
        ):
            raise PescApiError("PESC did not confirm that readings were accepted")

    async def async_submit_readings(
        self,
        account_id: int,
        meter_reference: str | None,
        readings: Mapping[str, Any],
        *,
        snapshot: PescSnapshot | None = None,
        confirm_high_consumption: bool = False,
        expected_period: str | None = None,
    ) -> PescSubmissionResult:
        """Validate and submit a complete meter reading set to PESC."""

        # A delayed confirmation must not use yesterday's cached AVAILABLE
        # state.  Fetch immediately before the write and fence the exact period
        # whose values were shown to the person in the original notification.
        current = (
            await self.async_get_snapshot()
            if confirm_high_consumption
            else snapshot or await self.async_get_snapshot()
        )
        try:
            account = current.accounts[int(account_id)]
        except (KeyError, ValueError) as exc:
            raise PescValidationError("The requested account was not found") from exc

        if account.reading_period and account.reading_period.forbidden:
            raise PescValidationError("PESC is not accepting readings for this account")
        if account.reading_state not in {None, "AVAILABLE"}:
            raise PescValidationError(
                f"PESC reading state is {account.reading_state}, not AVAILABLE"
            )

        if confirm_high_consumption:
            if getattr(self, "_account_type", None) != ACCOUNT_TYPE_LEGAL:
                raise PescValidationError(
                    "High-consumption confirmation requires a legal account"
                )
            period = account.reading_period
            if (
                not period
                or not period.name
                or not period.date_from
                or not period.date_to
            ):
                raise PescValidationError(
                    "PESC omitted the current legal reading period"
                )
            period_key = (
                f"{period.name}|{period.date_from}|{period.date_to}" if period else None
            )
            if not expected_period or expected_period != period_key:
                raise PescValidationError(
                    "PESC reading period changed or was not specified"
                )

        meter = self._find_meter(account, meter_reference)
        if meter.status not in {None, "ACTIVE"}:
            raise PescValidationError(f"The selected meter status is {meter.status}")
        payload, normalized_readings = self._scale_values(meter, readings)

        account_type = getattr(self, "_account_type", ACCOUNT_TYPE_INDIVIDUAL)
        if account_type == ACCOUNT_TYPE_LEGAL:
            await self._async_submit_legal_readings(
                account,
                meter,
                normalized_readings,
                confirm_high_consumption=confirm_high_consumption,
            )
            response: Any = True
        else:
            response = await self._request(
                "POST",
                (
                    f"v8/accounts/{account.account_id}/meters/"
                    f"{quote(meter.registration, safe='')}/reading"
                ),
                payload=payload,
            )
        accepted = (
            response is None
            or response is True
            or (isinstance(response, dict) and response.get("success") is True)
        )
        # Some API versions answer with an empty object on success.  HTTP status
        # validation has already happened, so treat that documented variant as
        # accepted while still returning an explicit boolean to the caller.
        if response == {}:
            accepted = True
        if not accepted:
            raise PescApiError("PESC did not confirm that readings were accepted")

        return PescSubmissionResult(
            account_id=account.account_id,
            meter_serial=meter.serial,
            readings=normalized_readings,
            accepted=True,
        )
