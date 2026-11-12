"""Redaction of secrets and personal data (F2).

Nothing may be indexed, logged or sent to a model before it has been through
``redact``. To make that checkable, redacted text has its own type,
``RedactedText``, and ``redact`` is the only function that creates one: code
that indexes, embeds or logs accepts ``RedactedText`` and nothing else.

The detectors are rules, not a model. They are tuned to over-redact rather than
leak, and they know street words for French, English and German addresses only.
Names and work email addresses are kept on purpose: later stages need to know
who did what.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import NewType

RedactedText = NewType("RedactedText", str)

KINDS = ("credential", "phone", "address", "email")

_VALUE = r"""[^\s'",;]+"""

PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
AUTHORIZATION = re.compile(rf"(?i)(\bauthorization:\s*(?:bearer|basic)\s+)({_VALUE})")
ASSIGNMENT = re.compile(
    r"(?i)([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key))"
    rf"""(["']?\s*[=:]\s*["']?)({_VALUE})"""
)
URL_USER_INFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
PHONE_INTERNATIONAL = re.compile(r"(?<![\w+])\+\d[\d\s().-]{7,18}\d")
PHONE_NORTH_AMERICA = re.compile(r"(?<![\d-])(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}(?![\d-])")
ADDRESS_FRENCH = re.compile(
    r"(?i)\b\d{1,4}(?:\s?(?:bis|ter))?,?\s+(?:rue|avenue|boulevard|chemin|impasse|allée)\s+"
    r"[^\n,.;:]{2,60}(?:,\s*\d{5}\s+[^\n,.;:]{2,40})?"
)
ADDRESS_ENGLISH = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][\w'-]*\s+){1,4}"
    r"(?:Street|St|Road|Rd|Lane|Ln|Drive|Dr|Avenue|Ave|Court|Ct|Way|Square|Boulevard|Blvd)\b\.?"
    r"(?:,\s*[A-Z][\w'-]*(?:\s+[A-Z0-9][\w'-]*){0,3})?"
)
ADDRESS_GERMAN = re.compile(
    r"\b[A-ZÄÖÜ][\wäöüß-]*(?:straße|strasse|str\.|weg|platz|allee|gasse)\s+\d{1,4}[a-z]?\b"
    r"(?:,\s*\d{5}\s+[^\n,.;:]{2,40})?"
)
ADDRESS_CONTEXT = re.compile(r"(?i)(\b(?:home address|private address|at home)\s*:\s*)([^\n.;]+)")
TOKEN = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_=-]{24,}(?![A-Za-z0-9+_=-])")
UUID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Redaction:
    text: RedactedText
    counts: dict[str, int]
    """How many spans of each kind were replaced. Never the values themselves."""


def placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _looks_like_a_secret(token: str) -> bool:
    """A long token that mixes digits with at least two other character classes."""
    if UUID.fullmatch(token) or not any(character.isdigit() for character in token):
        return False
    classes = (
        any(character.islower() for character in token),
        any(character.isupper() for character in token),
        any(character in "+_=-" for character in token),
    )
    return sum(classes) >= 2


def _digit_count(text: str) -> int:
    return sum(character.isdigit() for character in text)


def redact(text: str, *, work_email_domains: frozenset[str]) -> Redaction:
    """Replace secrets and personal data with typed placeholders.

    Idempotent on the text: redacting already redacted text changes nothing.
    """
    counts = dict.fromkeys(KINDS, 0)
    domains = frozenset(domain.lower() for domain in work_email_domains)

    def replace(
        pattern: re.Pattern[str], kind: str, build: Callable[[re.Match[str]], str | None]
    ) -> None:
        nonlocal text

        def substitute(match: re.Match[str]) -> str:
            replacement = build(match)
            if replacement is None or replacement == match.group(0):
                return match.group(0)
            counts[kind] += 1
            return replacement

        text = pattern.sub(substitute, text)

    credential, phone = placeholder("credential"), placeholder("phone")
    address, email = placeholder("address"), placeholder("email")

    replace(PRIVATE_KEY, "credential", lambda m: credential)
    replace(AUTHORIZATION, "credential", lambda m: m.group(1) + credential)
    replace(ASSIGNMENT, "credential", lambda m: m.group(1) + m.group(2) + credential)
    replace(URL_USER_INFO, "credential", lambda m: m.group(1) + credential + "@")
    replace(EMAIL, "email", lambda m: None if m.group(1).lower() in domains else email)
    replace(
        PHONE_INTERNATIONAL,
        "phone",
        lambda m: phone if 9 <= _digit_count(m.group(0)) <= 15 else None,
    )
    replace(PHONE_NORTH_AMERICA, "phone", lambda m: phone)
    replace(ADDRESS_FRENCH, "address", lambda m: address)
    replace(ADDRESS_ENGLISH, "address", lambda m: address)
    replace(ADDRESS_GERMAN, "address", lambda m: address)
    replace(ADDRESS_CONTEXT, "address", lambda m: m.group(1) + address)
    replace(TOKEN, "credential", lambda m: credential if _looks_like_a_secret(m.group(0)) else None)

    return Redaction(text=RedactedText(text), counts=counts)
