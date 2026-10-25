"""Outcome-ranked agent search (runtime/api/agent_search.py).

LABELLED is the answer key: a request someone would type when they want a new
agent, and the existing agent whose OUTCOME is closest. Wording is deliberately
different from the agents' own text, so a plain substring search fails most of
these. Runs against spec/examples, no database, no model.
"""
from __future__ import annotations

import pytest

from runtime.api import agent_search as s
from runtime.resolver import load_catalog

LABELLED: dict[str, str] = {
    "catch secrets committed in pull requests": "pr-reviewer",
    "find leaked API keys in a block of text": "secret-scanner",
    "answer employee questions about PTO in Slack": "people-ops-bot",
    "a bot that answers HR handbook questions": "people-ops-bot",
    "review pull requests for bugs without false alarms": "code-reviewer",
    "review terraform changes in infra PRs": "pr-reviewer",
    "make my agent cheaper without failing its evals": "cost-optimizer",
    "explain why runs keep failing and suggest a fix": "failure-triage",
    "draft a new agent from a plain description": "agent-builder",
    "check an agent has least privilege before publishing": "security-reviewer",
    "grade production runs for task success": "evaluator",
    "secrets": "pr-reviewer",
}


@pytest.fixture(scope="module")
def docs():
    cat = load_catalog()
    return [cat.latest(n) for n in sorted(cat.agents)]


@pytest.mark.parametrize("query,want", LABELLED.items())
def test_closest_outcome_is_in_the_top_three(docs, query, want):
    assert want in [r["name"] for r in s.rank(query, docs)][:3]


def test_top1_accuracy(docs):
    hits = sum(1 for q, want in LABELLED.items() if [r["name"] for r in s.rank(q, docs)][:1] == [want])
    assert hits >= 10, f"top-1 {hits}/{len(LABELLED)}"


def test_the_outcome_card_leaves_out_implementation(docs):
    pr = next(d for d in docs if d["metadata"]["name"] == "pr-reviewer")
    card = " ".join(s.outcome_card(pr).values())
    assert "leaked secrets are always flagged" in card
    assert "qwen" not in card and "Call each tool once" not in card and "0.25" not in card


def test_wording_differs_but_the_outcome_matches(docs):
    """'leaked keys' never appears in secret-scanner's text; 'credential' does."""
    hit = next(r for r in s.rank("report committed credentials", docs) if r["name"] == "secret-scanner")
    assert "credential" in hit["reason"]


def test_measured_results_only_break_ties(docs):
    q = "review pull requests for bugs without false alarms"
    plain = [r["name"] for r in s.rank(q, docs)]
    # the runner-up gets perfect numbers, the winner the worst: order must hold
    rigged = {plain[0]: {"pass_rate": 0.0, "production_success_rate": 0.0},
              plain[1]: {"pass_rate": 1.0, "production_success_rate": 1.0}}
    assert [r["name"] for r in s.rank(q, docs, rigged)][:2] == plain[:2]
    # ...but between two equal matches, the better-measured one wins
    a, b = docs[0], dict(docs[0], metadata=dict(docs[0]["metadata"], name="twin"))
    first = s.rank("draft agent", [a, b], {"twin": {"pass_rate": 0.9}, a["metadata"]["name"]: {"pass_rate": 0.4}})
    assert first[0]["name"] == "twin"


@pytest.mark.parametrize("word,stem", [("request", "request"), ("requests", "request"),
                                       ("cheapest", "cheap"), ("committed", "commit"),
                                       ("flagged", "flag"), ("queries", "query"),
                                       ("answers", "answer"), ("reviewers", "review"),
                                       ("builder", "build")])
def test_stemming(word, stem):
    assert s._stem(word) == stem


def test_nonsense_finds_nothing(docs):
    assert s.rank("zzqx flibbertigibbet", docs) == []
    assert s.rank("", docs) == []


def test_rewrite_failure_falls_back_to_the_raw_query(monkeypatch):
    monkeypatch.setenv("REAGENT_MODEL", "live")

    def boom(q):
        raise RuntimeError("no network")
    monkeypatch.setattr(s, "_rewrite_cached", boom)
    q = "a bot that answers on-call schedule questions in slack"
    assert s.expand_query(q) == q
    monkeypatch.setattr(s, "_rewrite_cached", lambda q: "Questions answered in Slack.")
    assert s.expand_query(q).endswith("Questions answered in Slack.")
    assert s.expand_query("secrets") == "secrets", "short keyword queries are never rewritten"
    monkeypatch.setenv("REAGENT_MODEL", "fake")
    assert s.expand_query(q) == q


# -- over HTTP ----------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from runtime.api.agents import create_app
    return TestClient(create_app())


def _get(client, user, q):
    return client.get("/v0/agents", params={"q": q}, headers={"X-ReAgent-User": user}).json()["agents"]


def test_http_ranks_by_outcome_and_says_why(client):
    got = _get(client, "frank", "catch secrets committed in pull requests")
    assert got[0]["name"] == "pr-reviewer"
    assert got[0]["match"]["reason"].startswith("Matched outcome:")
    assert got[0]["match"]["score"] >= got[-1]["match"]["score"]


def test_http_search_never_reveals_another_teams_agent(client):
    """bob (platform-eng) asks for exactly what people-ops-bot does."""
    names = [a["name"] for a in _get(client, "bob", "answer HR handbook questions in Slack")]
    assert "people-ops-bot" not in names and "agent-builder" not in names
    assert [a["name"] for a in _get(client, "dana", "answer HR handbook questions in Slack")] == \
        ["people-ops-bot"]
