"""F11: credentials fail closed; identity is supplied by secret configuration."""

import json

import pytest

from eki.auth import AuthenticationError, TokenAuthenticator
from eki.config import ConfigError

TOKEN = "test-only-owner-bearer-credential-0001"


def test_valid_and_invalid_credentials() -> None:
    auth = TokenAuthenticator(
        json.dumps([{"token": TOKEN, "subject": "owner", "name": "Nadia Ferrand"}])
    )
    identity = auth.authenticate("Bearer " + TOKEN)
    assert (identity.subject, identity.name, identity.admin) == ("owner", "Nadia Ferrand", False)
    for header in (None, "", "Basic " + TOKEN, "Bearer wrong", "Bearer "):
        with pytest.raises(AuthenticationError):
            auth.authenticate(header)
    assert TOKEN not in repr(auth)


@pytest.mark.parametrize(
    "config",
    [
        "oops",
        "{}",
        '[{"token":"short"}]',
        '[{"token":"test-only-owner-bearer-credential-0001","subject":"s","name":"N","admin":"false"}]',
        '[{"token":"test-only-owner-bearer-credential-0001","subject":"s","name":"N"},'
        '{"token":"test-only-owner-bearer-credential-0001","subject":"t","name":"M"}]',
    ],
)
def test_bad_configuration_never_echoes_credentials(config: str) -> None:
    with pytest.raises(ConfigError) as error:
        TokenAuthenticator(config)
    assert TOKEN not in str(error.value)


def test_empty_configuration_authenticates_nobody() -> None:
    with pytest.raises(AuthenticationError):
        TokenAuthenticator("[]").authenticate("Bearer " + TOKEN)


def test_every_data_endpoint_requires_credentials_and_documents_bearer_auth() -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app

    app = create_app(authenticator=TokenAuthenticator("[]"))
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    client = TestClient(app)
    for path, operations in schema["paths"].items():
        if path == "/healthz":
            continue
        url = path
        for parameter in ("run_id", "plan_id", "session_id", "entry_id"):
            url = url.replace("{" + parameter + "}", "00000000-0000-0000-0000-000000000000")
        for method, operation in operations.items():
            assert operation["security"] == [{"BearerAuth": []}]
            assert client.request(method, url).status_code == 401, (method, url)
