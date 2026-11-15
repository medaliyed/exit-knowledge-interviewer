"""The one list of provider credentials that opt-in live tests accept.

A live test makes paid calls only when one of these is exported to the test
process. A ``.env`` entry alone is not treated as consent.
"""

import os

PROVIDER_KEYS = (
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "AZURE_API_KEY",
    "COHERE_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
)


def has_provider_key() -> bool:
    return any(os.environ.get(key) for key in PROVIDER_KEYS)
