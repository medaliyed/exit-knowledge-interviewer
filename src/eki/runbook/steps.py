"""F9: cut what the expert said about a procedure into its steps.

A step ends where the expert ended a sentence, or where the expert's own list
says so. Nothing is reworded: joining the steps gives the text back, apart
from list markers ("1.", "-") that the runbook's own numbering replaces.

Deciding where a sentence ends is a matter of judgement at abbreviations. The
rule here prefers leaving two steps joined over cutting one in half: a joined
step still reads correctly, half a step does not.
"""

import re

# Never the end of a step: what follows belongs to the same sentence.
_NEVER_FINAL = frozenset(
    {"e.g.", "eg.", "i.e.", "ie.", "approx.", "vs.", "cf.", "ca.", "incl.", "excl."}
    | {"dr.", "mr.", "mrs.", "ms.", "prof.", "st."}
)
# May end a step: they do so only when the next word starts with a capital letter.
_MAY_BE_FINAL = frozenset(
    {"etc.", "min.", "max.", "sec.", "hr.", "hrs.", "inc.", "ltd.", "co.", "jr.", "sr."}
)
_NUMBER_FOLLOWS = frozenset({"no.", "nr."})
_LETTERS_AND_DOTS = re.compile(r"(?:[a-z]\.){2,}")  # a.m., u.s.
_INITIAL = re.compile(r"[A-Z]\.")
_OPENERS = "([\"'"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_NUMBER_MARK = re.compile(r"(?<!\S)(\d{1,2})[.)]\s+")
_LONE_NUMBER = re.compile(r"\d{1,2}[.)]")
_BULLET = re.compile(r"[-*•]\s+")


def _continues(so_far: str, following: str) -> bool:
    """Whether the full stop at the end of ``so_far`` is inside a sentence, not at its end."""
    token = so_far.rsplit(None, 1)[-1].lstrip(_OPENERS)
    if not token.endswith("."):
        return False
    if so_far == token and _LONE_NUMBER.fullmatch(token):
        return True  # "2. is ..." at the start of a line: a marker, never a step of its own
    word, first = token.lower(), following[:1]
    if word in _NEVER_FINAL:
        return True
    if word in _NUMBER_FOLLOWS:
        return first.isdigit()
    if _INITIAL.fullmatch(token):
        return first.isupper()  # "T. Brandl"
    if word in _MAY_BE_FINAL or _LETTERS_AND_DOTS.fullmatch(word):
        return not first.isupper()
    return False


def _sentences(line: str) -> list[str]:
    found: list[str] = []
    start = 0
    for gap in _SENTENCE_END.finditer(line):
        so_far = line[start : gap.start()].strip()
        if so_far and not _continues(so_far, line[gap.end() :]):
            found.append(so_far)
            start = gap.end()
    rest = line[start:].strip()
    return [*found, rest] if rest else found


def _numbered(text: str) -> tuple[str, ...] | None:
    """The items of a list the expert numbered 1, 2, 3 ..., or None when there is no such list.

    A marker counts only at the start of the text, at the start of a line, or after the
    end of a sentence, so the "2." in "set workers to 2." is not taken for one.
    """
    marks: list[re.Match[str]] = []
    for mark in _NUMBER_MARK.finditer(text):
        if int(mark.group(1)) != len(marks) + 1:
            continue
        before = text[: mark.start()].rstrip(" \t")
        at_start = not before
        after_boundary = before.endswith("\n") or before[-1:] in ".!?:;"
        if at_start or (marks and after_boundary):
            marks.append(mark)
    if len(marks) < 2:
        return None
    ends = [mark.start() for mark in marks[1:]] + [len(text)]
    items = (text[mark.end() : end].strip() for mark, end in zip(marks, ends, strict=True))
    return tuple(item for item in items if item)


def split_steps(text: str) -> tuple[str, ...]:
    """The steps of a procedure, in order, in the expert's words."""
    text = text.strip()
    if not text:
        return ()
    numbered = _numbered(text)
    if numbered is not None:
        return numbered
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and all(_BULLET.match(line) for line in lines):
        return tuple(_BULLET.sub("", line, count=1).strip() for line in lines)
    return tuple(step for line in lines for step in _sentences(line))


def claim_texts(section: str, text: str) -> tuple[str, ...]:
    """What one note becomes in a runbook section: steps are cut up, everything else is whole."""
    return split_steps(text) if section == "steps" else (text,)
