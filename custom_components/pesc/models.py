"""Typed data models returned by the PESC API client.

The upstream API is not documented publicly and returns several loosely typed
JSON structures.  Converting those structures at the API boundary keeps Home
Assistant entities independent from small upstream naming changes and makes it
possible to validate meter submissions before they leave the instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class PescMeterScale:
    """One tariff/register scale of a physical meter."""

    scale_id: int
    name: str
    previous_reading: float | None
    previous_reading_date: date | None
    unit: str | None
    # Legal-account submissions refer to the register and indication separately
    # from the user-facing tariff name.  Individual accounts leave these fields
    # empty and continue to submit only ``scale_id`` as before.
    register_id: str | None = None
    indication_id: int | None = None
    upstream_name: str | None = None
    time_of_use: str | None = None
    current_reading: float | None = None


@dataclass(frozen=True, slots=True)
class PescMeter:
    """A meter associated with one PESC account."""

    registration: str
    provider_id: int
    serial: str
    name: str
    status: str | None
    digits_left: int | None
    digits_right: int | None
    scales: tuple[PescMeterScale, ...]


@dataclass(frozen=True, slots=True)
class PescReadingPeriod:
    """Current period in which the account accepts meter readings."""

    name: str | None
    date_from: date | None
    date_to: date | None
    deadline_day: int | None
    forbidden: bool
    message: str | None


@dataclass(frozen=True, slots=True)
class PescAccount:
    """One account/object discovered inside the signed-in personal account."""

    account_id: int
    alias: str
    account_number: str | None
    provider_code: str
    auto_payment_on: bool
    delivery: str | None
    reading_state: str | None
    reading_period: PescReadingPeriod | None
    balance: float | None
    amount_due: float | None
    overpayment: float | None
    current_bill_amount: float | None
    current_bill_timestamp: datetime | None
    meters: tuple[PescMeter, ...] = field(default_factory=tuple)
    account_type: str = "individual"


@dataclass(frozen=True, slots=True)
class PescSnapshot:
    """Atomic coordinator snapshot for every configured PESC account."""

    accounts: dict[int, PescAccount]


@dataclass(frozen=True, slots=True)
class PescSubmissionResult:
    """Sanitized result returned to a Home Assistant action caller."""

    account_id: int
    meter_serial: str
    readings: dict[str, float]
    accepted: bool
