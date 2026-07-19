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
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    DEFAULT_PROVIDER_CODES,
    PESC_API_BASE_URL,
    PESC_CUSTOMER_HEADER,
    PESC_FRONTEND_VERSION,
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


class PescAuthenticationError(PescApiError):
    """Raised when PESC credentials or the TOTP challenge are rejected."""


class PescConnectionError(PescApiError):
    """Raised when the PESC endpoint cannot be reached."""


class PescValidationError(PescApiError):
    """Raised before an unsafe or malformed meter submission is transmitted."""


def normalize_login(value: str) -> tuple[str, str]:
    """Normalize an email or Russian phone login for the PESC login endpoint."""

    login = value.strip()
    if "@" in login:
        return login.lower(), "EMAIL"

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
    """Authenticated, retrying client for one PESC personal account."""

    def __init__(
        self,
        session: ClientSession,
        login: str,
        password: str,
        totp_secret: str,
        *,
        provider_codes: list[str] | None = None,
    ) -> None:
        self._session = session
        self._login, self._login_type = normalize_login(login)
        self._password = password
        self._totp_secret = totp_secret
        self._provider_codes = {
            code.upper() for code in (provider_codes or DEFAULT_PROVIDER_CODES)
        }
        self._tokens: dict[str, str] | None = None
        self._auth_lock = asyncio.Lock()

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
            "customer": PESC_CUSTOMER_HEADER,
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
                f"{PESC_API_BASE_URL}/{path.lstrip('/')}",
                json=payload,
                headers=self._headers(authenticated=authenticated),
            ) as response:
                status = response.status
                data = await self._response_json(response)
        except (ClientError, TimeoutError) as exc:
            raise PescConnectionError("Unable to connect to PESC") from exc

        if status not in allowed:
            error = self._error_message(data, status)
            if status in {401, 403, 424}:
                raise PescAuthenticationError(error)
            raise PescApiError(error)
        return status, data

    async def async_authenticate(self) -> None:
        """Create a session using login, password, and an automatic TOTP code."""

        async with self._auth_lock:
            login_headers = self._headers(authenticated=False)
            login_headers.update({"Captcha": "none", "withTotp": "true"})
            try:
                async with self._session.post(
                    f"{PESC_API_BASE_URL}/v8/users/auth",
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
                return

            if status != 424 or not isinstance(payload, dict):
                raise PescAuthenticationError(self._error_message(payload, status))

            transaction_id = payload.get("transactionId")
            challenge_types = payload.get("types", [])
            if not isinstance(transaction_id, str) or "TOTP" not in challenge_types:
                raise PescAuthenticationError("PESC did not offer a TOTP challenge")

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
        accounts = await self._request("GET", "v8/accounts/light")
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
    def _meters(payload: Any) -> tuple[PescMeter, ...]:
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
                            raw_scale.get("scaleName") or raw_scale["meterScaleId"]
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
                            str(raw_scale["unit"])
                            if raw_scale.get("unit") is not None
                            else None
                        ),
                    )
                )
            meters.append(
                PescMeter(
                    registration=str(raw_id["registration"]),
                    provider_id=int(raw_id.get("provider") or 0),
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

        raw_accounts = await self._request("GET", "v8/accounts/light")
        if not isinstance(raw_accounts, list):
            raise PescApiError("PESC returned an invalid account list")

        selected_accounts = [
            account
            for account in raw_accounts
            if isinstance(account, dict)
            and self._provider_code(account) in self._provider_codes
        ]
        accounts = await asyncio.gather(
            *(self._async_account_snapshot(account) for account in selected_accounts)
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
        expected_keys = {str(scale.scale_id).casefold() for scale in meter.scales} | {
            scale.name.strip().casefold() for scale in meter.scales
        }
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

    async def async_submit_readings(
        self,
        account_id: int,
        meter_reference: str | None,
        readings: Mapping[str, Any],
        *,
        snapshot: PescSnapshot | None = None,
    ) -> PescSubmissionResult:
        """Validate and submit a complete meter reading set to PESC."""

        current = snapshot or await self.async_get_snapshot()
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

        meter = self._find_meter(account, meter_reference)
        if meter.status not in {None, "ACTIVE"}:
            raise PescValidationError(f"The selected meter status is {meter.status}")
        payload, normalized_readings = self._scale_values(meter, readings)

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
