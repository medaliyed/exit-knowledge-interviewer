"""Configured bearer identities. Tokens never become principals or database values."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from eki.config import ConfigError
from eki.runbook.models import NonBlank


class AuthenticationError(Exception):
    """Missing or invalid credentials (no credential content in the message)."""


class ForbiddenError(Exception):
    """The authenticated identity cannot perform this action."""


@dataclass(frozen=True)
class Principal:
    subject: str
    name: str
    admin: bool = False


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class _Credential(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: SecretStr
    subject: NonBlank
    name: NonBlank
    admin: bool = False


class TokenAuthenticator:
    def __init__(self, configuration: str) -> None:
        self._credentials: list[tuple[bytes, Principal]] = []
        try:
            data = json.loads(configuration)
            if not isinstance(data, list):
                raise ValueError
            seen: set[bytes] = set()
            identities: dict[str, Principal] = {}
            for item in data:
                value = _Credential.model_validate(item)
                token = value.token.get_secret_value()
                if len(token) < 32 or any(c.isspace() for c in token):
                    raise ValueError
                digest = hashlib.sha256(token.encode()).digest()
                identity = Principal(value.subject, value.name, value.admin)
                if digest in seen or identities.get(identity.subject, identity) != identity:
                    raise ValueError
                seen.add(digest)
                identities[identity.subject] = identity
                self._credentials.append((digest, identity))
        except (ValueError, TypeError, ValidationError) as error:
            raise ConfigError(
                "EKI_API_TOKENS must be valid credential JSON with unique tokens"
            ) from error

    @property
    def identities(self) -> tuple[Principal, ...]:
        """Unique public identities for consistency checks; never expose credentials."""
        return tuple(dict.fromkeys(identity for _, identity in self._credentials))

    def authenticate(self, authorization: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or token.strip() != token:
            raise AuthenticationError("valid bearer credentials required")
        digest = hashlib.sha256(token.encode()).digest()
        found = None
        for expected, identity in self._credentials:
            if hmac.compare_digest(expected, digest):
                found = identity
        if found is None:
            raise AuthenticationError("valid bearer credentials required")
        return found
