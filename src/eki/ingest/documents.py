"""Helpers for wiki pages."""

import re

PATH_PATTERN = re.compile(r"`([\w.-]+(?:/[\w.-]+)+)`")


def paths_mentioned(body: str) -> set[str]:
    """File paths a page mentions in backticks, such as `deploy/cron.yaml`."""
    return set(PATH_PATTERN.findall(body))
