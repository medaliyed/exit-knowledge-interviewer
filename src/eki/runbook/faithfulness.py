"""F9: does a claim say more than the interview turn it cites?

The interviewer model writes the notes, and synthesis copies them into claims
that cite the expert. Nothing else stands between a note the model worded
wrongly, or padded with a detail of its own, and a claim that reads as the
expert's. This check compares each claim with the turns it cites and names
the words and numbers that are not there.

It is a comparison of words, not of meaning, and it uses no model:

- a number must appear as written; other words match after light stemming;
- common function words are ignored, negations are not;
- the question the expert was answering counts as a source too, because
  "Yes" confirms what the question said.

So a faithful paraphrase is pointed out as well, and a wrong statement built
from the answer's own words is not. The result informs the reviewer; it never
blocks or changes anything.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from eki.interview.models import Turn
from eki.runbook.models import SECTION_NAMES, InterviewTurnCitation, RunbookEntry, SectionName

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NEGATIONS = frozenset({"no", "not", "never", "none", "nor"})
# Function words: too common to say anything about where a claim came from.
_IGNORED_WORDS = (
    "a an the and or but if then than so to of in on at by for from with into onto over "
    "under after before during until while when where which who whom whose what why how "
    "that this these those there here it its they them their we our you your he she his "
    "her my me is are was were be been being am do does did done have has had will would "
    "can could should must may might shall as also about up down out off"
)
_IGNORED = frozenset(_IGNORED_WORDS.split())
_SUFFIXES = ("ing", "ed", "es", "s")


def _tokens(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower().replace("\u2019", "'"))
    return ["not" if w.endswith("n't") or w == "cannot" else w.split("'")[0] for w in words]


def _stem(word: str) -> str:
    """Enough to match restart, restarts, restarted and restarting. Not a real stemmer."""
    if word.isalpha() and len(word) > 3:
        for suffix in _SUFFIXES:
            stem = word[: -len(suffix)]
            if word.endswith(suffix) and len(stem) >= 3 and not word.endswith("ss"):
                word = stem[:-1] if stem[-1] == stem[-2] and stem[-1] not in "aeiouls" else stem
                break
        if len(word) > 3 and word[-1] == "e":
            word = word[:-1]
        elif len(word) > 3 and word[-1] == "y":
            word = word[:-1] + "i"
    return word


def _checked(word: str) -> bool:
    if word in _NEGATIONS or any(c.isdigit() for c in word):
        return True
    return len(word) >= 3 and word not in _IGNORED


def unsupported_terms(claim: str, sources: Iterable[str]) -> tuple[str, ...]:
    """The claim's words and numbers found in none of the sources, in order, once each."""
    said = {_stem(word) for source in sources for word in _tokens(source)}
    missing = [w for w in _tokens(claim) if _checked(w) and _stem(w) not in said]
    return tuple(dict.fromkeys(missing))


@dataclass(frozen=True)
class Unsupported:
    """A claim with terms its cited turns do not contain. The terms are the claim's own."""

    section: SectionName
    index: int
    terms: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.section}[{self.index}]: not in the cited answer: " + ", ".join(self.terms)


def _key(session: str) -> str:
    try:
        return str(UUID(session))
    except ValueError:
        return session


def _sources(
    claim_citations: Sequence[object], turns_for: Callable[[str], Mapping[int, Turn] | None]
) -> list[str] | None:
    """What the cited turns say, or None when one of them cannot be compared.

    Each citation resolves against the transcript of the session it names. A
    confidential turn is never read here, so nothing about it can show in a result.
    """
    texts: list[str] = []
    for citation in claim_citations:
        if not isinstance(citation, InterviewTurnCitation):
            return None
        turns = turns_for(citation.session_id)
        if turns is None:
            return None
        turn = turns.get(citation.turn_index)
        if turn is None or turn.confidential or turn.speaker not in ("expert", "maintainer"):
            return None
        texts.append(turn.text)
        asked = turns.get(turn.answers) if turn.answers is not None else None
        if asked is not None and asked.kind == "question":
            texts.append(asked.text)
    return texts or None


def check_entry(
    entry: RunbookEntry,
    turns: Iterable[Turn],
    *,
    session_id: str | None = None,
    linked: Mapping[str, Iterable[Turn]] | None = None,
) -> tuple[Unsupported, ...]:
    """Every claim that says something its cited turns do not, in template order.

    ``turns`` is the transcript of the entry's own session, ``session_id``. Citations of
    a session in ``linked`` (explicitly linked follow-ups) resolve against that session's
    transcript, so equal ordinals in different sessions never mix. Without
    ``session_id`` every other citation resolves against ``turns``, as before. Claims
    that cite an artifact, an unknown session, or a confidential or absent turn are left
    out.
    """
    own = {turn.ordinal: turn for turn in turns}
    by_session = {_key(s): {t.ordinal: t for t in ts} for s, ts in (linked or {}).items()}
    home = None if session_id is None else _key(session_id)

    def turns_for(cited: str) -> Mapping[int, Turn] | None:
        key = _key(cited)
        if key in by_session:
            return by_session[key]
        return own if home is None or key == home else None

    found: list[Unsupported] = []
    for name in SECTION_NAMES:
        for index, claim in enumerate(entry.section(name).claims):
            sources = _sources(claim.citations, turns_for)
            if sources is None:
                continue
            terms = unsupported_terms(claim.text, sources)
            if terms:
                found.append(Unsupported(name, index, terms))
    return tuple(found)
