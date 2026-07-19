"""RFC 6238 test vectors for local TOTP generation."""

import pytest

from custom_components.pesc.totp import InvalidTotpSecret, generate_totp


def test_rfc6238_sha1_vector() -> None:
    """Generate the RFC example at Unix timestamp 59 seconds."""

    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert (
        generate_totp(f"otpauth://totp/Test?secret={secret}&digits=8", timestamp=59)
        == "94287082"
    )


def test_plain_secret_defaults_to_six_digits() -> None:
    """Accept the compact Base32 form used by manual setup forms."""

    code = generate_totp("JBSWY3DPEHPK3PXP", timestamp=0)
    assert len(code) == 6
    assert code.isdigit()


def test_invalid_uri_is_rejected() -> None:
    """Reject HOTP so a non-rotating second factor is never assumed."""

    with pytest.raises(InvalidTotpSecret):
        generate_totp("otpauth://hotp/Test?secret=JBSWY3DPEHPK3PXP")
