"""H1 over HTTP: GET /resolved and POST /validate, filtered by the caller (SPEC.md §2)."""
from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

from runtime.api.agents import create_app  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts" / "resolved_spec.schema.json").read_text())
client = TestClient(create_app())


def get(name, user):
    return client.get(f"/v0/agents/{name}/resolved", headers={"X-ReAgent-User": user} if user else {})


@pytest.mark.parametrize("name,user", [("pr-reviewer", "alice"), ("people-ops-bot", "dana"),
                                       ("pr-reviewer", "frank")])
def test_resolved_validates_against_the_contract(name, user):
    r = get(name, user)
    assert r.status_code == 200
    assert not list(Draft202012Validator(SCHEMA).iter_errors(r.json()))


def test_member_cannot_see_another_teams_agent_or_enterprise_configs():
    assert get("people-ops-bot", "alice").status_code == 404
    assert get("secret-scanner", "alice").status_code == 404
    assert get("secret-scanner", "frank").status_code == 200


def test_no_principal_is_refused():
    assert get("pr-reviewer", None).status_code == 403


def test_validate_returns_rejections_naming_the_field():
    text = (ROOT / "spec/examples/agents/platform-eng/pr-reviewer.agent").read_text(encoding="utf-8")
    ok = client.post("/v0/agents/pr-reviewer/validate", json={"spec": text},
                     headers={"X-ReAgent-User": "alice"})
    assert ok.status_code == 200 and ok.json() == {"valid": True, "rejections": []}
    bad = client.post("/v0/agents/pr-reviewer/validate",
                      json={"spec": text.replace("timeout_seconds: 240", "timeout_seconds: 1200")},
                      headers={"X-ReAgent-User": "alice"})
    body = bad.json()
    assert body["valid"] is False
    assert body["rejections"][0]["field"] == "spec.budget.timeout_seconds"
    assert body["rejections"][0]["blocked_by"] == "enterprise"


def test_publish_pipeline_over_http():
    pytest.importorskip("langchain")
    from runtime.publish import STORE
    STORE.runs.clear()
    H = {"X-ReAgent-User": "alice"}
    r = client.post("/v0/agents/pr-reviewer/publish", headers=H)
    assert r.status_code == 202 and r.json()["review_url"].endswith(f"run={r.json()['publish_run_id']}")
    rid = r.json()["publish_run_id"]
    assert client.post("/v0/agents/pr-reviewer/publish", headers=H).status_code == 409
    assert client.post(f"/v0/publish-runs/{rid}/continue", headers=H).status_code == 409
    dry = client.post(f"/v0/publish-runs/{rid}/cases/agent/never-merges/dry-run", headers=H).json()
    assert dry["passed"] and dry["output"]["blocked_calls"] == ["github:pulls.merge"]
    assert client.get(f"/v0/publish-runs/{rid}", headers={"X-ReAgent-User": "dana"}).status_code == 404
    for c in client.get(f"/v0/publish-runs/{rid}", headers=H).json()["cases"]:
        if c["decision"] is None:
            ok = client.post(f"/v0/publish-runs/{rid}/cases/{c['case_id']}/review",
                             json={"decision": "approve"}, headers=H)
            assert ok.status_code == 200
    done = client.post(f"/v0/publish-runs/{rid}/continue", headers=H).json()
    assert done["status"] in ("failed", "succeeded") and done["stage"] in ("gate", "publish")
    STORE.runs.clear()


def test_every_wf2_route_is_served_by_the_runtime_app():
    """runtime/api/runs.py mounts `router` only. A second router would 404 on port 8000."""
    pytest.importorskip("langchain")
    from runtime.api.runs import app
    from runtime.publish import STORE
    STORE.runs.clear()
    c, H = TestClient(app), {"X-ReAgent-User": "alice"}
    assert c.get("/v0/agents/pr-reviewer/resolved", headers=H).status_code == 200
    rid = c.post("/v0/agents/pr-reviewer/publish", headers=H).json()["publish_run_id"]
    assert c.get(f"/v0/publish-runs/{rid}", headers=H).status_code == 200
    assert c.post(f"/v0/publish-runs/{rid}/continue", headers=H).status_code == 409
    STORE.runs.clear()


def test_fork_into_own_team_records_the_exact_source_version():
    from runtime.resolver import source
    source._DRAFTS.clear()
    H = {"X-ReAgent-User": "bob"}
    r = client.post("/v0/agents/code-reviewer/fork", json={"new_name": "bobs-reviewer"}, headers=H)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["forked_from"] == "code-reviewer@1.2.0" and body["team"] == "platform-eng"
    assert body["rejections"] == [] and "forked_from: code-reviewer@1.2.0" in body["spec_yaml"]
    # the fork is a real agent now: it resolves by name
    assert get("bobs-reviewer", "bob").status_code == 200
    assert client.post("/v0/agents/code-reviewer/fork", json={"new_name": "bobs-reviewer"},
                       headers=H).status_code == 409
    assert client.post("/v0/agents/people-ops-bot/fork", json={"new_name": "x"},
                       headers=H).status_code == 404                 # not visible to platform-eng
    assert client.post("/v0/agents/code-reviewer/fork", json={"new_name": "Bad Name"},
                       headers=H).status_code == 422
    source._DRAFTS.clear()


def test_new_agent_forks_the_teams_base_config():
    """Starting from scratch is forking the team's base config: it inherits the
    enterprise and team layers and adds nothing, not even tools."""
    from runtime.resolver import source
    source._DRAFTS.clear()
    H = {"X-ReAgent-User": "alice"}
    r = client.post("/v0/agents/team:platform-eng/fork", json={"new_name": "fresh-agent"}, headers=H)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["team"] == "platform-eng" and body["forked_from"] is None and body["rejections"] == []
    doc = get("fresh-agent", "alice").json()
    assert [p["source"] for p in doc["provenance"]["prompt"]] == ["enterprise", "team:platform-eng", "agent"]
    assert not [t for t in doc["effective"]["tool_permissions"] if t["connector"] != "memory"]
    assert client.post("/v0/agents/team:people-ops/fork", json={"new_name": "x"}, headers=H).status_code == 404
    source._DRAFTS.clear()


def test_already_running_names_the_pipeline_and_it_can_be_abandoned():
    pytest.importorskip("langchain")
    from runtime.publish import STORE
    STORE.runs.clear()
    H = {"X-ReAgent-User": "alice"}
    rid = client.post("/v0/agents/pr-reviewer/publish", headers=H).json()["publish_run_id"]
    refused = client.post("/v0/agents/pr-reviewer/publish", headers=H)
    rej = refused.json()["rejections"][0]
    assert refused.status_code == 409 and rej["publish_run_id"] == rid and rej["stage"] == "review"
    assert rej["message"].startswith("A publish pipeline is already running for this agent")
    steps = client.get(f"/v0/publish-runs/{rid}", headers=H).json()["steps"]
    assert [s["status"] for s in steps] == ["passed", "passed", "passed", "waiting", "pending", "pending", "pending"]
    assert client.post(f"/v0/publish-runs/{rid}/abandon", headers=H).json()["status"] == "abandoned"
    assert client.post("/v0/agents/pr-reviewer/publish", headers=H).status_code == 202
    STORE.runs.clear()


def listing(user, **params):
    r = client.get("/v0/agents", params=params, headers={"X-ReAgent-User": user})
    assert r.status_code == 200
    return {a["name"]: a for a in r.json()["agents"]}


def test_collection_is_filtered_to_the_callers_team():
    alice = listing("alice")
    assert alice and all(a["team"] == "platform-eng" and a["level"] == "team" for a in alice.values())
    assert "people-ops-bot" not in alice and "secret-scanner" not in alice
    assert {"people-ops-bot", "secret-scanner", "pr-reviewer"} <= set(listing("frank"))
    for a in alice.values():
        assert {"name", "version", "description"} <= set(a) and isinstance(a["runs_30d"], int)


def test_collection_search_and_tag_filter():
    assert "pr-reviewer" in listing("alice", q="terraform")
    assert listing("alice", q="no-such-outcome-anywhere") == {}
    tagged = listing("alice", tag="security")
    assert tagged and all("security" in a["tags"] for a in tagged.values())


def test_agent_detail_carries_the_file_and_versions():
    H = {"X-ReAgent-User": "alice"}
    r = client.get("/v0/agents/pr-reviewer", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "kind: Agent" in body["spec_yaml"] and body["level"] == "team"
    assert body["versions"][0]["version"] == body["version"]
    assert client.get("/v0/agents/people-ops-bot", headers=H).status_code == 404
    assert client.get("/v0/agents/secret-scanner", headers=H).status_code == 404
    assert client.get("/v0/agents/pr-reviewer").status_code == 403


def test_every_route_the_dashboard_calls_is_served_by_the_runtime_app():
    """The dashboard falls back to recorded fixtures on a 404 'Not Found', so a
    missing route looks fine on screen. Catch it here instead."""
    import re
    pytest.importorskip("langchain")
    from runtime.api.runs import app
    client_ts = (ROOT / "dashboard" / "lib" / "api.ts").read_text(encoding="utf-8")
    called = set(re.findall(r'api\.(GET|POST|PUT|DELETE)\("([^"]+)"', client_ts))
    assert called
    served = {(m.upper(), re.sub(r"\{[a-z_]+\}", "{}", path))
              for path, ops in app.openapi()["paths"].items() for m in ops}
    missing = sorted(f"{m} {p}" for m, p in called if (m, re.sub(r"\{[a-z_]+\}", "{}", p)) not in served)
    assert not missing, f"dashboard calls routes the runtime does not serve: {missing}"
