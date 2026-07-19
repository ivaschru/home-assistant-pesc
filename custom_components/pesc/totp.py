"""Small RFC 6238 TOTP implementation used for unattended PESC re-login.

The integration intentionally implements the tiny algorithm with Python's
standard library instead of adding a runtime dependency only for one function.
The function accepts either a Base32 secret or a standard ``otpauth://`` URI.
No caller should log the input value because it is equivalent to a second
authentication factor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse


class InvalidTotpSecret(ValueError):
    """Raised when a TOTP secret cannot be parsed safely."""


def _parse_totp_secret(value: str) -> tuple[str, str, int, int]:
    """Return ``secret, algorithm, digits, period`` without exposing the secret."""

    raw_value = value.strip()
    algorithm = "SHA1"
    digits = 6
    period = 30

    if raw_value.lower().startswith("otpauth://"):
        parsed = urlparse(raw_value)
        if parsed.scheme.lower() != "otpauth" or parsed.netloc.lower() != "totp":
            raise InvalidTotpSecret("Only otpauth TOTP URIs are supported")

        query = parse_qs(parsed.query)
        try:
            raw_value = query["secret"][0]
        except (KeyError, IndexError) as exc:
            raise InvalidTotpSecret("The otpauth URI has no secret") from exc

        algorithm = query.get("algorithm", [algorithm])[0].upper()
        try:
            digits = int(query.get("digits", [str(digits)])[0])
            period = int(query.get("period", [str(period)])[0])
        except ValueError as exc:
            raise InvalidTotpSecret("Invalid TOTP digits or period") from exc

    secret = "".join(raw_value.split()).upper()
    if not secret:
        raise InvalidTotpSecret("The TOTP secret is empty")
    if algorithm not in {"SHA1", "SHA256", "SHA512"}:
        raise InvalidTotpSecret("Unsupported TOTP algorithm")
    if digits not in {6, 7, 8}:
        raise InvalidTotpSecret("TOTP digits must be 6, 7, or 8")
    if period <= 0:
        raise InvalidTotpSecret("TOTP period must be positive")

    return secret, algorithm, digits, period


def generate_totp(value: str, *, timestamp: float | None = None) -> str:
    """Generate a current RFC 6238 code from a Base32 secret or otpauth URI."""

    secret, algorithm, digits, period = _parse_totp_secret(value)
    padded_secret = secret + "=" * ((8 - len(secret) % 8) % 8)
    try:
        key = base64.b32decode(padded_secret, casefold=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise InvalidTotpSecret("The TOTP secret is not valid Base32") from exc

    counter = int(time.time() if timestamp is None else timestamp) // period
    digest = hmac.new(
        key,
        struct.pack(">Q", counter),
        getattr(hashlib, algorithm.lower()),
    ).digest()
    offset = digest[-1] & 0x0F
    binary_code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary_code % (10**digits):0{digits}d}"
