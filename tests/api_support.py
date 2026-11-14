"""Explicit trusted identity for legacy tests whose subject is interview behavior."""

from typing import Any

from fastapi import FastAPI

from eki.api.app import create_app as build_app
from eki.auth import Principal


class TrustedTestAuth:
    def authenticate(self, authorization: str | None) -> Principal:
        return Principal("test-owner", "Nadia Ferrand", True)


def create_app(*args: Any, **kwargs: Any) -> FastAPI:
    app = build_app(*args, authenticator=TrustedTestAuth(), **kwargs)
    return app
