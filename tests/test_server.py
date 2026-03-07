from starlette.testclient import TestClient

from pyphony.models import (
    AgentTotals,
    Issue,
    LiveSession,
    OrchestratorRuntimeState,
    RetryEntry,
    RunAttempt,
    RunningEntry,
)
from pyphony.server import create_app


def _sample_state() -> OrchestratorRuntimeState:
    issue = Issue(id="issue-1", identifier="PROJ-42", title="Fix bug", state="In Progress")
    attempt = RunAttempt(issue_id="issue-1", issue_identifier="PROJ-42", attempt=1, status="running")
    session = LiveSession(session_id="sess-1", turn_count=3)
    entry = RunningEntry(issue=issue, attempt=attempt, session=session)
    retry = RetryEntry(issue_id="issue-2", identifier="PROJ-99", attempt=2, error="timeout")
    return OrchestratorRuntimeState(
        running={"issue-1": entry},
        retry_attempts={"issue-2": retry},
        agent_totals=AgentTotals(input_tokens=100, output_tokens=50, total_tokens=150, seconds_running=12.5),
    )


def test_dashboard_returns_html():
    app = create_app()
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_api_state_returns_default_keys():
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/v1/state")
    assert response.status_code == 200
    data = response.json()
    assert "running" in data
    assert "retrying" in data
    assert "agent_totals" in data
    assert data["running"] == []
    assert data["retrying"] == []


def test_api_state_with_running_entries():
    state = _sample_state()
    app = create_app(get_state_fn=lambda: state)
    client = TestClient(app)
    response = client.get("/api/v1/state")
    assert response.status_code == 200
    data = response.json()
    assert len(data["running"]) == 1
    assert data["running"][0]["issue_id"] == "issue-1"
    assert data["running"][0]["issue_identifier"] == "PROJ-42"
    assert data["running"][0]["state"] == "In Progress"
    assert data["running"][0]["turn_count"] == 3
    assert len(data["retrying"]) == 1
    assert data["retrying"][0]["issue_id"] == "issue-2"
    assert data["retrying"][0]["identifier"] == "PROJ-99"
    assert data["retrying"][0]["attempt"] == 2
    assert data["retrying"][0]["error"] == "timeout"
    assert data["agent_totals"]["input_tokens"] == 100
    assert data["agent_totals"]["output_tokens"] == 50
    assert data["agent_totals"]["total_tokens"] == 150
    assert data["agent_totals"]["seconds_running"] == 12.5


def test_api_issue_found():
    state = _sample_state()
    app = create_app(get_state_fn=lambda: state)
    client = TestClient(app)
    response = client.get("/api/v1/PROJ-42")
    assert response.status_code == 200
    data = response.json()
    assert "issue" in data
    assert "attempt" in data
    assert "session" in data
    assert data["issue"]["identifier"] == "PROJ-42"


def test_api_issue_not_found():
    state = _sample_state()
    app = create_app(get_state_fn=lambda: state)
    client = TestClient(app)
    response = client.get("/api/v1/PROJ-999")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_api_issue_not_found_no_state_fn():
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/v1/PROJ-1")
    assert response.status_code == 404


def test_api_refresh_returns_202():
    app = create_app()
    client = TestClient(app)
    response = client.post("/api/v1/refresh")
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
