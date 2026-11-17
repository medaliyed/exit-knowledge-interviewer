"""Exit codes and the error printer shared by the ``eki`` commands."""

import sys

USAGE_ERROR = 2


FAILURE = 1


def fail(message: str, code: int = FAILURE) -> int:
    print(f"eki: {message}", file=sys.stderr)
    return code
