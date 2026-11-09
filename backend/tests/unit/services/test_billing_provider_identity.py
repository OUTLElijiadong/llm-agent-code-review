import pytest

from app.services.billing_ledger_service import (
    BillingError,
    resolve_billing_provider_identity,
    validate_billing_provider_identity,
)


@pytest.mark.parametrize("source", ["system", "global"])
def test_official_deepseek_platform_credentials_resolve_to_sanitized_identity(source):
    identity = resolve_billing_provider_identity(
        provider_name="deepseek", base_url="https://api.deepseek.com/v1/",
        credential_source=source, model_name="deepseek-v4-flash",
    )

    assert (identity.identity_version, identity.provider_name, identity.endpoint_host,
            identity.credential_source) == ("deepseek-official-v1", "deepseek", "api.deepseek.com", source)
    assert "api_key" not in identity.__dict__


@pytest.mark.parametrize(("provider", "base_url", "source", "model"), [
    ("deepseek", "https://api.deepseek.com/v1", "user", "deepseek-v4-flash"),
    ("openai-compatible", "https://api.deepseek.com/v1", "global", "deepseek-v4-flash"),
    ("deepseek", "https://proxy.example/v1", "global", "deepseek-v4-flash"),
    ("deepseek", "http://api.deepseek.com/v1", "system", "deepseek-v4-flash"),
    ("deepseek", "https://api.deepseek.com.evil.example/v1", "system", "deepseek-v4-flash"),
    ("deepseek", "https://api.deepseek.com:8443/v1", "system", "deepseek-v4-flash"),
    ("deepseek", "https://user@api.deepseek.com/v1", "system", "deepseek-v4-flash"),
    ("deepseek", "https://api.deepseek.com/v1?route=proxy", "system", "deepseek-v4-flash"),
    ("deepseek", "https://api.deepseek.com/v1/responses", "system", "deepseek-v4-flash"),
    ("deepseek", "https://api.deepseek.com/v1", "system", "deepseek-v4-pro"),
])
def test_unverified_provider_endpoint_credential_or_model_fails_closed(provider, base_url, source, model):
    with pytest.raises(BillingError):
        resolve_billing_provider_identity(
            provider_name=provider, base_url=base_url,
            credential_source=source, model_name=model,
        )


def test_manually_constructed_untrusted_identity_is_rejected():
    from app.services.billing_ledger_service import BillingProviderIdentity

    with pytest.raises(BillingError, match="白名单"):
        validate_billing_provider_identity(BillingProviderIdentity(
            identity_version="deepseek-official-v1", provider_name="deepseek",
            endpoint_host="proxy.example", credential_source="global",
        ))
