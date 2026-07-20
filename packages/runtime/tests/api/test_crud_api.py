"""API integration tests for the CRUD endpoints (agents/todos/workflows/etc).

Note: the FastAPI lifespan seeds default agents (dev/research/writer), 7 todos,
and the pr-triage workflow into the isolated home on startup.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


# ------------------------------ agents ------------------------------ #
def test_agents_seeded(client):
    ids = {a["id"] for a in client.get("/agents").json()}
    assert {"dev", "research", "writer"} <= ids


def test_agent_create_update_delete(client):
    r = client.post("/agents", json={"id": "qa", "name": "QA"}).json()
    assert r["ok"] and r["agent"]["id"] == "qa"
    r = client.put("/agents/qa", json={"name": "QA2"}).json()
    assert r["agent"]["name"] == "QA2"
    assert client.delete("/agents/qa").json()["ok"] is True


def test_agent_duplicate_returns_error(client):
    r = client.post("/agents", json={"id": "dev", "name": "dup"}).json()
    assert r["ok"] is False and "already exists" in r["error"]


# ------------------------------ todos ------------------------------ #
def test_todos_seeded_by_lifespan(client):
    assert len(client.get("/todos").json()) == 7


def test_todo_crud(client):
    before = len(client.get("/todos").json())
    todo = client.post("/todos", json={"title": "API task", "priority": "high"}).json()["todo"]
    assert todo["id"]
    assert len(client.get("/todos").json()) == before + 1
    upd = client.patch(f"/todos/{todo['id']}", json={"done": True}).json()
    assert upd["todo"]["done"] is True
    assert client.delete(f"/todos/{todo['id']}").json()["ok"] is True


def test_todo_create_requires_title(client):
    r = client.post("/todos", json={"title": ""}).json()
    assert r["ok"] is False


def test_todo_patch_unknown(client):
    r = client.patch("/todos/nope", json={"done": True}).json()
    assert r["ok"] is False


# ---------------------------- workflows ---------------------------- #
def test_workflows_seeded(client):
    ids = {w["id"] for w in client.get("/workflows").json()}
    assert "pr-triage" in ids


def test_workflow_create_and_delete(client):
    wf = client.post(
        "/workflows",
        json={"name": "Release", "description": "d", "steps": [{"title": "a"}, {"title": "b"}]},
    ).json()["workflow"]
    assert len(wf["steps"]) == 2
    assert client.delete(f"/workflows/{wf['id']}").json()["ok"] is True


def test_workflow_runs_list(client):
    assert isinstance(client.get("/workflow_runs").json(), list)


# ---------------------------- providers ---------------------------- #
def test_providers_get(client):
    r = client.get("/providers").json()
    assert "default_provider" in r
    assert "providers" in r and "custom" in r["providers"]


def test_providers_put_and_default(client):
    r = client.put(
        "/providers",
        json={"providers": {"custom": {"enabled": True, "api_key": "k"}}, "default_provider": "custom"},
    ).json()
    assert r["ok"] is True
    assert r["default_provider"] == "custom"
    # round-trips
    assert client.get("/providers").json()["providers"]["custom"]["enabled"] is True


def test_provider_verify_endpoint_never_500s(client):
    r = client.post("/providers/does-not-exist/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ------------------------------ skills ------------------------------ #
def test_skill_create_list_body_delete(client):
    body = "---\nname: hello\ndescription: say hi\n---\n# Body\nSay hello.\n"
    assert client.post("/skills", json={"name": "hello", "body": body}).json()["ok"] is True
    names = {s["name"] for s in client.get("/skills").json()}
    assert "hello" in names
    assert "Say hello." in client.get("/skills/hello/body").json()["body"]
    assert client.delete("/skills/hello").json()["ok"] is True


def test_skill_create_requires_name(client):
    r = client.post("/skills", json={"name": "", "body": "x"}).json()
    assert r["ok"] is False


# --------------------------- mcp / kb --------------------------- #
def test_mcp_config_get_put(client):
    assert client.get("/mcp/config").json() == {"mcpServers": {}}
    cfg = {"mcpServers": {"vault": {"transport": "stdio", "command": "x"}}}
    assert client.put("/mcp", json=cfg).json()["ok"] is True
    assert "vault" in client.get("/mcp/config").json()["mcpServers"]


def test_mcp_empty_registry(client):
    r = client.get("/mcp").json()
    assert r == {"servers": [], "tools": []}


def test_kb_servers_empty(client):
    assert client.get("/kb/servers").json() == []


# ---------------------------- artifacts ---------------------------- #
def test_artifacts_initially_empty(client):
    assert client.get("/artifacts?project_slug=default").json() == []
