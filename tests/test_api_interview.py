"""Task P2-h: plan review and the interview chat over HTTP, with an SSE stream per message."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from eki.graph.workflow import Limits
from eki.interview import store
from eki.llm import ModelError, Reply
from tests.api_support import create_app
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.interview_support import TOPIC_IDS, make_service, seed_run
from tests.support import FAKE_CREDENTIALS
from tests.test_interviewer import ask, note

pytestmark = pytest.mark.db


def events(response: Any) -> list[dict[str, Any]]:
    """Parse an SSE body into its events, checking that each is named after its type."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    parsed = []
    for block in response.text.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if "data" not in fields:
            continue
        event = json.loads(fields["data"])
        assert fields["event"] == event["type"]
        parsed.append(event)
    return parsed


class Api:
    def __init__(
        self, scratch: Scratch, script: list[Reply | Exception] | None = None, **limits: int
    ):
        with scratch.connect() as conn:
            self.run_id = seed_run(conn)
        self.scratch = scratch
        self.model = ScriptedModel(script or [], name="interviewer")
        options: dict[str, Any] = {"limits": Limits(**limits)}
        if script is not None:
            options |= {"agent": None, "interviewer": self.model}
        self.client = TestClient(create_app(make_service(scratch.dsn, **options)))

    def plan(self) -> dict[str, Any]:
        response = self.client.post(f"/runs/{self.run_id}/plan")
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    def approve(self, plan_id: str) -> dict[str, Any]:
        reviewed = self.client.get(f"/plans/{plan_id}").json()
        response = self.client.post(
            f"/plans/{plan_id}/approve",
            json={"approved_by": "Nadia Ferrand", "expected_revision": reviewed["revision"]},
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def say(self, session_id: str, **message: Any) -> list[dict[str, Any]]:
        return events(self.client.post(f"/sessions/{session_id}/messages", json=message))


@pytest.fixture
def api(scratch: Scratch) -> Iterator[Api]:
    yield Api(scratch)


def test_full_plan_review_flow(api: Api) -> None:
    plan = api.plan()
    assert plan["status"] == "draft"
    assert [t["topic_id"] for t in plan["topics"]] == list(TOPIC_IDS)
    question = plan["topics"][0]["questions"][0]
    assert question["artifact_id"] == "TCK-0001"
    assert question["quote"].startswith("Nadia Ferrand: Restarted the ledger-db replicas")
    plan_id = plan["id"]
    assert api.client.get(f"/plans/{plan_id}").json() == plan

    a, b, c = TOPIC_IDS
    added = {"label": "Audit handover", "note": "Ask me who signs it off.", "position": 0}
    edited = api.client.patch(
        f"/plans/{plan_id}",
        json={
            "remove": [b],
            "add": [added],
            "order": [c, "expert:audit-handover", a],
            "expected_revision": plan["revision"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert [t["topic_id"] for t in edited.json()["topics"]] == [c, "expert:audit-handover", a]
    assert edited.json()["topics"][1]["questions"][0]["text"] == "Ask me who signs it off."

    session = api.approve(plan_id)
    assert session["status"] == "active"
    assert session["current_topic_id"] == c
    assert session["waiting"]["turn"] == 1
    assert session["plan_id"] == plan_id
    assert api.client.get(f"/plans/{plan_id}").json()["approved_by"] == "Nadia Ferrand"
    assert api.client.get(f"/sessions/{session['id']}").json() == session


@pytest.mark.parametrize("action", ["edit", "approve"])
def test_stale_original_plan_cannot_be_edited_or_approved(api: Api, action: str) -> None:
    reviewed = api.plan()
    path = f"/plans/{reviewed['id']}"
    changed = api.client.patch(
        path, json={"remove": [TOPIC_IDS[-1]], "expected_revision": reviewed["revision"]}
    )
    assert changed.status_code == 200, changed.text
    if action == "edit":
        refused = api.client.patch(
            path, json={"remove": [TOPIC_IDS[0]], "expected_revision": reviewed["revision"]}
        )
    else:
        refused = api.client.post(
            f"{path}/approve",
            json={"approved_by": "Nadia Ferrand", "expected_revision": reviewed["revision"]},
        )
    assert refused.status_code == 409, refused.text
    current = api.client.get(path).json()
    assert current == changed.json()
    assert current["revision"] == reviewed["revision"] + 1
    with api.scratch.connect() as conn:
        assert store.session_of_plan(conn, UUID(reviewed["id"])) is None


@pytest.mark.parametrize("revision", [None, 0, -1])
@pytest.mark.parametrize("action", ["edit", "approve"])
def test_plan_mutations_require_a_positive_revision(
    api: Api, action: str, revision: int | None
) -> None:
    plan = api.plan()
    path = f"/plans/{plan['id']}"
    body: dict[str, Any] = {} if revision is None else {"expected_revision": revision}
    if action == "edit":
        response = api.client.patch(path, json={**body, "remove": [TOPIC_IDS[0]]})
    else:
        response = api.client.post(f"{path}/approve", json={**body, "approved_by": "Nadia Ferrand"})
    assert response.status_code == 422, response.text
    assert api.client.get(path).json() == plan


def test_plan_edit_errors(api: Api) -> None:
    plan = api.plan()
    plan_id = plan["id"]
    expected = {"expected_revision": plan["revision"]}
    assert (
        api.client.patch(f"/plans/{plan_id}", json={**expected, "remove": ["nope"]}).status_code
        == 422
    )
    assert (
        api.client.patch(
            f"/plans/{plan_id}", json={**expected, "order": [TOPIC_IDS[0]]}
        ).status_code
        == 422
    )
    assert api.client.patch(f"/plans/{plan_id}", json={**expected, "unknown": 1}).status_code == 422
    blank = {**expected, "add": [{"label": " ", "note": "x"}]}
    assert api.client.patch(f"/plans/{plan_id}", json=blank).status_code == 422
    assert (
        api.client.post(
            f"/plans/{plan_id}/approve", json={**expected, "approved_by": " "}
        ).status_code
        == 422
    )
    assert len(api.client.get(f"/plans/{plan_id}").json()["topics"]) == 3, (
        "a refused edit changes nothing"
    )


def test_an_edit_to_an_approved_plan_gives_409(api: Api) -> None:
    plan_id = api.plan()["id"]
    api.approve(plan_id)
    expected = {"expected_revision": api.client.get(f"/plans/{plan_id}").json()["revision"]}
    assert (
        api.client.patch(
            f"/plans/{plan_id}", json={**expected, "remove": [TOPIC_IDS[0]]}
        ).status_code
        == 409
    )
    again = api.client.post(
        f"/plans/{plan_id}/approve", json={**expected, "approved_by": "Nadia Ferrand"}
    )
    assert again.status_code == 409


def test_unknown_ids_give_404(api: Api) -> None:
    missing = uuid4()
    client = api.client
    assert client.post(f"/runs/{missing}/plan").status_code == 404
    assert client.get(f"/plans/{missing}").status_code == 404
    assert client.patch(f"/plans/{missing}", json={"expected_revision": 1}).status_code == 404
    assert (
        client.post(
            f"/plans/{missing}/approve",
            json={"approved_by": "Nadia Ferrand", "expected_revision": 1},
        ).status_code
        == 404
    )
    assert client.get(f"/sessions/{missing}").status_code == 404
    message = {"action": "answer", "text": "hi"}
    assert client.post(f"/sessions/{missing}/messages", json=message).status_code == 404
    assert client.get("/plans/not-a-uuid").status_code == 422


def test_unmapped_run_gives_409(api: Api) -> None:
    with api.scratch.connect() as conn:
        unmapped = seed_run(conn, mapped=False)
    response = api.client.post(f"/runs/{unmapped}/plan")
    assert response.status_code == 409
    assert "gap scores" in response.json()["detail"]


def test_a_message_streams_events_and_ends_on_a_question(scratch: Scratch) -> None:
    api = Api(
        scratch,
        [
            tool_reply(ask("What triggers it?")),
            tool_reply(note("trigger", "The batch aborts."), ask("And then?", "steps")),
        ],
        plan_size=1,
    )
    session = api.approve(api.plan()["id"])
    assert session["waiting"] == {
        "turn": 1,
        "question": "What triggers it?",
        "section": "trigger",
        "topic_id": TOPIC_IDS[0],
    }
    stream = api.say(session["id"], action="answer", text="The batch aborts.")
    assert [e["type"] for e in stream] == ["note", "question"]
    assert stream[0]["section"] == "trigger"
    assert stream[-1] == {
        "type": "question",
        "turn": 3,
        "question": "And then?",
        "section": "steps",
        "topic_id": TOPIC_IDS[0],
    }
    view = api.client.get(f"/sessions/{session['id']}").json()
    assert view["checklist"]["trigger"] == "noted"
    assert [(t["ordinal"], t["speaker"], t["kind"]) for t in view["turns"]] == [
        (1, "interviewer", "question"),
        (2, "expert", "answer"),
        (3, "interviewer", "question"),
    ]
    assert view["notes"] == [
        {
            "topic_id": TOPIC_IDS[0],
            "section": "trigger",
            "text": "The batch aborts.",
            "turn": 2,
            "confidential": False,
        }
    ]


def test_the_interview_ends_with_done_and_then_refuses_answers(api: Api) -> None:
    session = api.approve(api.plan()["id"])
    last: list[dict[str, Any]] = []
    for n in range(3):
        last = api.say(session["id"], action="answer", text=f"Answer {n}.")
    assert last == [{"type": "done"}]
    view = api.client.get(f"/sessions/{session['id']}").json()
    assert (view["status"], view["waiting"], view["current_topic_id"]) == ("finished", None, None)
    late = api.client.post(f"/sessions/{session['id']}/messages", json={"text": "More."})
    assert late.status_code == 409


def test_model_failure_leaves_session_resumable(scratch: Scratch) -> None:
    api = Api(
        scratch,
        [
            tool_reply(ask("What triggers it?")),
            ModelError("down"),
            tool_reply(note("trigger"), ask("And then?", "steps")),
        ],
        plan_size=1,
    )
    session = api.approve(api.plan()["id"])
    failed = api.say(session["id"], action="answer", text="The batch aborts.")
    assert [e["type"] for e in failed] == ["error"]
    assert failed[0]["code"] == "model_unavailable"
    view = api.client.get(f"/sessions/{session['id']}").json()
    assert view["status"] == "active"
    assert len(view["turns"]) == 2
    retried = api.say(session["id"], action="answer", text="The batch aborts.")
    assert [e["type"] for e in retried] == ["note", "question"]
    turns = api.client.get(f"/sessions/{session['id']}").json()["turns"]
    assert [t["ordinal"] for t in turns] == [1, 2, 3], "retrying did not duplicate a turn"


def test_model_failure_while_planning_gives_502(scratch: Scratch) -> None:
    with scratch.connect() as conn:
        run_id = seed_run(conn)
    service = make_service(scratch.dsn, drafter=ScriptedModel([ModelError("down")]))
    response = TestClient(create_app(service)).post(f"/runs/{run_id}/plan")
    assert response.status_code == 502
    assert "model" in response.json()["detail"].lower()


def test_concurrent_messages_conflict(api: Api) -> None:
    """While one message is being handled, a second one for the same session is refused."""
    session = api.approve(api.plan()["id"])
    with api.scratch.connect() as other:
        assert store.try_lock(other, UUID(session["plan_id"]))
        busy = api.client.post(f"/sessions/{session['id']}/messages", json={"text": "Second."})
        assert busy.status_code == 409
        assert "being handled" in busy.json()["detail"]
    assert api.say(session["id"], action="answer", text="First.")[-1]["type"] == "question"


def test_bad_messages_give_422_and_store_nothing(api: Api) -> None:
    session = api.approve(api.plan()["id"])
    url = f"/sessions/{session['id']}/messages"
    for body in (
        {"action": "answer", "text": "  "},
        {"action": "shout", "text": "x"},
        {"action": "correct", "text": "x"},
        {"action": "correct", "text": "x", "corrects": 1},
        {"action": "answer", "text": "x", "extra": True},
    ):
        assert api.client.post(url, json=body).status_code == 422, body
    assert (
        api.client.post(url, json={"action": "correct", "text": "x", "corrects": 9}).status_code
        == 404
    )
    assert len(api.client.get(f"/sessions/{session['id']}").json()["turns"]) == 1


def test_skip_confidential_and_correct_over_http(scratch: Scratch) -> None:
    api = Api(
        scratch,
        [
            tool_reply(ask("Who do you call?", "contacts")),
            tool_reply(ask("What triggers it?")),
            tool_reply(note("trigger", "It aborts."), ask("Steps?", "steps")),
            tool_reply(ask("Rollback?", "rollback")),
        ],
        plan_size=1,
    )
    session = api.approve(api.plan()["id"])
    sid = session["id"]
    confidential = api.say(sid, action="answer", text="Marek Dolan, privately.", confidential=True)
    assert confidential[0] == {
        "type": "note",
        "topic_id": TOPIC_IDS[0],
        "section": "contacts",
        "confidential": True,
    }
    api.say(sid, action="answer", text="It aborts.")
    corrected = api.say(sid, action="correct", text="It aborts in the posting phase.", corrects=4)
    assert corrected[0] == {"type": "corrected", "turn": 6, "corrects": 4, "sections": ["trigger"]}
    skipped = api.say(sid, action="skip")
    assert skipped[0]["type"] == "open"
    body = api.client.get(f"/sessions/{sid}").text
    assert "Marek" not in body, "a confidential answer is not echoed by the API"
    view = json.loads(body)
    assert view["turns"][1] == {
        "ordinal": 2,
        "topic_id": TOPIC_IDS[0],
        "speaker": "expert",
        "kind": "answer",
        "text": "[confidential]",
        "confidential": True,
        "section": None,
        "answers": 1,
        "corrects": None,
    }
    assert view["checklist"] == {
        "trigger": "noted",
        "diagnosis": "missing",
        "steps": "open",
        "rollback": "missing",
        "contacts": "confidential",
        "pitfalls": "missing",
        "rationale": "missing",
    }
    assert view["open_items"][0]["reason"] == "skipped by expert"


def test_no_response_body_contains_a_planted_secret(scratch: Scratch) -> None:
    leak = f"password={FAKE_CREDENTIALS[1]}"
    api = Api(
        scratch,
        [
            tool_reply(ask(f"Is it still {leak}?", "steps")),
            tool_reply(note("steps", f"She said {leak} works"), ask("And then?", "trigger")),
        ],
        plan_size=1,
    )
    plan = api.plan()
    patch = {
        "add": [{"label": f"About {leak}", "note": f"Ask about {leak}"}],
        "expected_revision": plan["revision"],
    }
    bodies = [json.dumps(plan), api.client.patch(f"/plans/{plan['id']}", json=patch).text]
    session = api.approve(plan["id"])
    bodies.append(json.dumps(session))
    bodies.append(
        api.client.post(f"/sessions/{session['id']}/messages", json={"text": f"Yes, {leak}"}).text
    )
    bodies.append(api.client.get(f"/sessions/{session['id']}").text)
    bodies.append(api.client.get(f"/plans/{plan['id']}").text)
    for body in bodies:
        assert FAKE_CREDENTIALS[1] not in body
    assert "[REDACTED:credential]" in bodies[1]
    assert "[REDACTED:credential]" in bodies[3]


def test_healthz_still_works(api: Api) -> None:
    assert api.client.get("/healthz").json() == {"status": "ok"}


def test_without_model_settings_the_api_says_so(
    monkeypatch: pytest.MonkeyPatch, scratch: Scratch
) -> None:
    from eki.config import get_settings

    for name in ("EKI_MODEL_INTERVIEWER", "EKI_MODEL_DRAFTER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    monkeypatch.chdir("/")
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).get(f"/plans/{uuid4()}")
        assert response.status_code == 503
        assert "EKI_MODEL_DRAFTER is not set" in response.json()["detail"]
    finally:
        get_settings.cache_clear()
