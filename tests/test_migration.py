"""Cover recovery without dropping credentials, identity, or future schemas."""

import pytest

from custom_components.pesc.migration import (
    PescEntryMigrationError,
    migrated_entry_data,
)


def saved_credentials():
    """Use synthetic strings, never real provider credentials or tokens."""

    return {
        "account_type": "legal",
        "username": "synthetic-organization",
        "password": "synthetic-password",
        "totp_secret": "synthetic-totp",
        "provider_codes": ["PSK"],
        "unrelated_option": {"keep": True},
    }


def test_foreign_v2_removes_only_injected_metadata():
    original = saved_credentials()
    affected = {**original, "auth": {"auth": None}, "login_type": "PHONE"}
    result = migrated_entry_data(affected, 2)
    assert result == original
    assert affected["auth"] == {"auth": None}
    assert affected["login_type"] == "PHONE"


@pytest.mark.parametrize("version", [1, 3])
def test_our_schemas_preserve_exact_credentials_and_settings(version):
    data = saved_credentials()
    assert migrated_entry_data(data, version) == data
    assert migrated_entry_data(data, version) is not data


def test_individual_account_without_explicit_type_stays_compatible():
    data = saved_credentials()
    data.pop("account_type")
    assert migrated_entry_data(data, 1) == data


@pytest.mark.parametrize("key", ["username", "password", "totp_secret"])
@pytest.mark.parametrize("invalid", [None, "", "   ", 42])
def test_incomplete_foreign_configuration_is_rejected(key, invalid):
    data = {**saved_credentials(), key: invalid, "auth": {"auth": "synthetic"}}
    with pytest.raises(PescEntryMigrationError):
        migrated_entry_data(data, 2)
    assert "auth" in data


@pytest.mark.parametrize("version", [0, 4])
def test_unknown_version_never_downgrades_or_changes_data(version):
    data = saved_credentials()
    with pytest.raises(PescEntryMigrationError):
        migrated_entry_data(data, version)
    assert data == saved_credentials()


def test_foreign_token_only_entry_is_not_silently_converted():
    with pytest.raises(PescEntryMigrationError):
        migrated_entry_data({"auth": {"verified": "synthetic"}}, 2)


def test_unknown_account_type_is_rejected():
    with pytest.raises(PescEntryMigrationError):
        migrated_entry_data({**saved_credentials(), "account_type": "other"}, 2)
