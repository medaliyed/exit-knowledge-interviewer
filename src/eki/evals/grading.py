"""Score one interviewed topic against its golden. Pure: a session view in, a result out.

Every check is deterministic string or structure matching. ``invented`` reuses
the faithfulness tokeniser so "the expert never said this" means the same thing
here as in entry review. Confidential turns are never read as support and never
copied into the result.
"""

from collections.abc import Sequence

from eki.evals.models import Check, Golden, NoteRecord, OpenRecord, ScenarioResult, TranscriptTurn
from eki.interview.models import Note, OpenItem, Turn
from eki.interview.service import SessionView
from eki.runbook.faithfulness import unsupported_terms

CONFIDENTIAL = "[confidential]"


def _sections(notes: list[Note], golden: Golden) -> Check:
    noted = {n.section for n in notes}
    missing = [s for s in golden.noted if s not in noted]
    return Check(
        name="sections",
        found=len(golden.noted) - len(missing),
        expected=len(golden.noted),
        passed=not missing,
        details=tuple(f"{s}: no note" for s in missing),
    )


def _facts(notes: list[Note], golden: Golden) -> Check:
    expected = sum(len(phrases) for phrases in golden.noted.values())
    missing: list[str] = []
    for section, phrases in golden.noted.items():
        texts = [n.text.lower() for n in notes if n.section == section and not n.confidential]
        missing += [
            f"{section}: '{phrase}' not in any note"
            for phrase in phrases
            if not any(phrase.lower() in text for text in texts)
        ]
    found = expected - len(missing)
    ratio = found / expected if expected else 1.0
    return Check(
        name="facts",
        found=found,
        expected=expected,
        passed=ratio >= golden.min_facts,
        details=tuple(missing),
    )


def _open(notes: list[Note], open_items: list[OpenItem], golden: Golden) -> Check:
    noted = {n.section for n in notes}
    left_open = {o.section for o in open_items}
    problems: list[str] = []
    for section in golden.open:
        if section in noted:
            problems.append(f"{section}: filled in")
        elif section not in left_open:
            problems.append(f"{section}: no open item")
    return Check(
        name="open",
        found=len(golden.open) - len(problems),
        expected=len(golden.open),
        passed=not problems,
        details=tuple(problems),
    )


def _follow_ups(questions: list[Turn], golden: Golden) -> Check:
    later = {q.section for q in questions[1:]}
    missing = [s for s in golden.follow_ups if s not in later]
    return Check(
        name="follow_ups",
        found=len(golden.follow_ups) - len(missing),
        expected=len(golden.follow_ups),
        passed=not missing,
        details=tuple(f"{s}: never asked after the first question" for s in missing),
    )


def _mentions(questions: list[Turn], golden: Golden) -> Check:
    missing = [
        group
        for group in golden.mentions
        if not any(all(i in q.text for i in group) for q in questions)
    ]
    return Check(
        name="mentions",
        found=len(golden.mentions) - len(missing),
        expected=len(golden.mentions),
        passed=not missing,
        details=tuple(f"{', '.join(group)}: not put to the expert together" for group in missing),
    )


def _invented(notes: list[Note], said: Sequence[str], golden: Golden) -> Check:
    invented: list[str] = []
    for note in notes:
        if note.confidential:
            continue
        terms = unsupported_terms(note.text, said)
        if len(terms) > golden.max_unsupported_terms:
            invented.append(f"{note.section}: {', '.join(terms)}")
    return Check(
        name="invented",
        found=len(invented),
        expected=0,
        passed=not invented,
        details=tuple(invented),
    )


def _questions(questions: list[Turn], golden: Golden) -> Check:
    count = len(questions)
    over = count > golden.max_questions
    return Check(
        name="questions",
        found=count,
        expected=golden.max_questions,
        passed=not over,
        details=(f"{count} questions, {golden.max_questions} allowed",) if over else (),
    )


def grade(view: SessionView, topic_id: str, golden: Golden) -> ScenarioResult:
    turns = [t for t in view.turns if t.topic_id == topic_id]
    questions = [t for t in turns if t.kind == "question"]
    notes = [n for n in view.notes if n.topic_id == topic_id and not n.superseded]
    open_items = [o for o in view.open_items if o.topic_id == topic_id]
    said = [t.text for t in turns if t.speaker == "expert" and not t.confidential]
    checks = (
        _sections(notes, golden),
        _facts(notes, golden),
        _open(notes, open_items, golden),
        _follow_ups(questions, golden),
        _mentions(questions, golden),
        _invented(notes, said, golden),
        _questions(questions, golden),
    )
    finished = view.session.status == "finished"
    return ScenarioResult(
        scenario=golden.scenario,
        passed=finished and all(c.passed for c in checks),
        finished=finished,
        checks=checks,
        questions=len(questions),
        transcript=tuple(
            TranscriptTurn(
                ordinal=t.ordinal,
                speaker=t.speaker,
                kind=t.kind,
                section=t.section,
                text=CONFIDENTIAL if t.confidential else t.text,
            )
            for t in turns
        ),
        notes=tuple(
            NoteRecord(
                section=n.section, text=CONFIDENTIAL if n.confidential else n.text, turn=n.turn
            )
            for n in notes
        ),
        open_items=tuple(OpenRecord(section=o.section, reason=o.reason) for o in open_items),
    )
