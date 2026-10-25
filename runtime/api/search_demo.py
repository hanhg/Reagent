"""Agent search demo: find the existing agent closest to the one you want. WF2.

    python -m runtime.api.search_demo            # files catalog, no database, no credits
    python -m runtime.api.search_demo --live     # + act 6, the gpt-oss-20b rewrite (~$0.0002)

Drives fixtures/search/scenario.yaml against the REAL runtime app, served by
uvicorn on a free local port and called over HTTP the way the dashboard calls
it (GET /v0/agents?q=, POST /v0/agents/{name}/fork) and the way agent-builder
calls it (the platform connector at /mcp/platform, through the same MCP client
and load_tools the runtime uses). Nothing is mocked except, in act 4, the
measured numbers that would otherwise come from the database.

PASS/FAIL lines are requirements; WARN lines do not fail the exit code.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import secrets
import socket
import sys
import threading
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "fixtures" / "search" / "scenario.yaml"

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
    level = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append((level, name, detail))
    print(f"  {level}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


# --------------------------------------------------------------------------
# The server, and calling it
# --------------------------------------------------------------------------

def serve() -> tuple[str, Any]:
    import uvicorn

    from runtime.api.runs import app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


class Api:
    def __init__(self, base: str) -> None:
        import httpx
        self.base, self.http = base, httpx.Client(base_url=base, timeout=30)

    def search(self, who: str, q: str, **params: Any) -> list[dict[str, Any]]:
        r = self.http.get("/v0/agents", params={"q": q, **params}, headers={"X-ReAgent-User": who})
        r.raise_for_status()
        return r.json()["agents"]

    def collection(self, who: str) -> list[str]:
        return [a["name"] for a in self.http.get("/v0/agents", headers={"X-ReAgent-User": who}).json()["agents"]]

    def fork(self, who: str, name: str, new_name: str) -> tuple[int, dict[str, Any]]:
        r = self.http.post(f"/v0/agents/{name}/fork", json={"new_name": new_name},
                           headers={"X-ReAgent-User": who})
        return r.status_code, r.json()


def names(hits: list[dict[str, Any]]) -> list[str]:
    return [h["name"] for h in hits]


def show(hits: list[dict[str, Any]], n: int = 3) -> None:
    for h in hits[:n]:
        m = h.get("match") or {}
        print(f"        {h['name']:<18} {m.get('score', 0):>7.2f}  {m.get('reason', '')[:96]}")


# --------------------------------------------------------------------------
# Acts
# --------------------------------------------------------------------------

def act_search(api: Api, scn: dict[str, Any]) -> None:
    print("\nAct 1: describe the outcome, get the agent that achieves it")
    for case in scn["search"]:
        hits = api.search(case["who"], case["query"])
        print(f'    {case["who"]}: "{case["query"]}"')
        show(hits)
        top = names(hits)[:1]
        reason = (hits[0].get("match") or {}).get("reason", "") if hits else ""
        ok = top == [case["expect_first"]] and all(w in reason for w in case.get("reason_has", []))
        check(f'{case["who"]} finds {case["expect_first"]}', ok,
              f"got {top[0] if top else 'nothing'}" + (f"; reason: {reason[:80]}" if case.get("reason_has") else ""))
    first = api.search("bob", scn["search"][0]["query"])
    check("every hit says why it matched, and the outcome is named first",
          bool(first) and all((h.get("match") or {}).get("reason", "").startswith("Matched outcome:")
                              for h in first[:1]))


def act_wording(api: Api, scn: dict[str, Any]) -> None:
    w = scn["wording"]
    print(f'\nAct 2: different words, same outcome\n    {w["who"]}: "{w["query"]}"')
    hits = api.search(w["who"], w["query"])
    show(hits)
    check(f'"{w["query"]}" reaches {w["expect_in_top3"]} (whose text says "credential")',
          w["expect_in_top3"] in names(hits)[:3], f"top 3: {names(hits)[:3]}")


def act_isolation(api: Api, scn: dict[str, Any]) -> None:
    i, n = scn["isolation"], scn["nothing"]
    print(f'\nAct 3: search never crosses a team boundary\n    {i["who"]}: "{i["query"]}"')
    hits = api.search(i["who"], i["query"])
    show(hits)
    leaked = sorted(set(names(hits)) & set(i["never"]))
    check(f'{i["who"]} never sees another team\'s or an enterprise agent', not leaked,
          f"leaked {leaked}" if leaked else
          f"people-ops-bot is exactly this, and bob gets {len(hits)} hit(s) from platform-eng only")
    check("a query that matches no outcome returns nothing, not everything",
          api.search(n["who"], n["query"]) == [])


def act_tie_break(api: Api, scn: dict[str, Any]) -> None:
    from runtime.api import agent_search
    t = scn["tie_break"]
    print(f'\nAct 4: measured results break ties, never outrank the right outcome\n    {t["who"]}: "{t["query"]}"')
    real = agent_search.measured_results
    agent_search.measured_results = lambda: t["measured"]  # type: ignore[assignment]
    try:
        hits = api.search(t["who"], t["query"])
    finally:
        agent_search.measured_results = real  # type: ignore[assignment]
    show(hits)
    by = {h["name"]: h for h in hits}
    check(f'{t["expect_first"]} stays first despite a {t["measured"][t["expect_first"]]["pass_rate"]:.0%} pass rate',
          names(hits)[:1] == [t["expect_first"]], f"order {names(hits)}")
    check("the measured numbers reach the result", all(
        by.get(n, {}).get("pass_rate") == m.get("pass_rate") for n, m in t["measured"].items() if n in by))


def act_fork(api: Api, scn: dict[str, Any]) -> None:
    f = scn["fork"]
    new = f"{f['new_name']}-{secrets.token_hex(2)}"
    print(f'\nAct 7: the New agent drawer. Describe it, take the suggestion, fork it\n'
          f'    {f["who"]}: "{f["describe"]}"')
    hits = [h for h in api.search(f["who"], f["describe"]) if h.get("team") == "platform-eng"][:3]
    show(hits)
    check(f'the drawer suggests {f["expect_suggestion"]}', names(hits)[:1] == [f["expect_suggestion"]],
          f"suggested {names(hits)}")
    status, body = api.fork(f["who"], f["expect_suggestion"], new)
    check(f"Fork this creates {new} as a draft that records where it came from",
          status == 201 and body.get("status") == "draft"
          and str(body.get("forked_from", "")).startswith(f["expect_suggestion"] + "@")
          and not body.get("rejections"), f"HTTP {status}, forked_from {body.get('forked_from')}")
    check("the fork is in the team's collection", new in api.collection(f["who"]))
    again = names(api.search(f["who"], f["describe"]))
    check("the next person describing the same outcome finds the fork too", new in again[:3],
          f"top 3: {again[:3]}")


def act_platform(base: str, scn: dict[str, Any]) -> None:
    from runtime.engine import binding
    from runtime.engine.runner import load_resolved
    from runtime.guardrails.permissions import GrantSet, connector_actions
    p = scn["platform"]
    print(f'\nAct 5: {p["agent"]} runs the same search over the platform connector')
    url = base + "/mcp/platform"
    spec = connector_actions()["connectors"]["platform"]
    saved = (spec["transport_url"], os.environ.get("REAGENT_CONNECTORS"))
    spec["transport_url"], os.environ["REAGENT_CONNECTORS"] = url, "live"
    try:
        grants = GrantSet.from_effective(load_resolved(p["agent"])["effective"])
        tools = asyncio.run(binding.load_tools(grants, {"X-Reagent-User": p["who"]}))
        tool = {t.name: t for t in tools}
        check(f'{p["agent"]} loads its {len(tool)} granted platform tools with no drift',
              "platform__search_agents" in tool, ", ".join(sorted(tool)))
        res = asyncio.run(tool["platform__search_agents"].ainvoke({"query": p["query"]}))
        text = res if isinstance(res, str) else "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in (res if isinstance(res, list) else [res]))
        got = json.loads(text)["agents"]
        print(f'    {p["agent"]} as {p["who"]}: platform:search_agents "{p["query"]}"')
        show(got)
        check(f'platform:search_agents ranks {p["expect_first"]} first, scoped to {p["who"]}',
              names(got)[:1] == [p["expect_first"]] and all(a["team"] == "platform-eng" for a in got))
        stub = asyncio.run(tool["platform__propose_agent"].ainvoke({}))
        check("an action not built yet says so instead of pretending",
              "E_NOT_AVAILABLE" in json.dumps(stub, default=str))
    finally:
        spec["transport_url"] = saved[0]
        if saved[1] is None:
            os.environ.pop("REAGENT_CONNECTORS", None)
        else:
            os.environ["REAGENT_CONNECTORS"] = saved[1]
    import httpx
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "search_agents", "arguments": {"query": "secrets"}}},
                   headers={"X-Reagent-User": p["who"], "Accept": "application/json, text/event-stream"})
    check("a call without the runtime's shared secret gets no agents",
          "E_PLATFORM_UNAUTHORIZED" in r.text and "pr-reviewer" not in r.text,
          "refused: E_PLATFORM_UNAUTHORIZED" if "E_PLATFORM_UNAUTHORIZED" in r.text else r.text[:120])


def act_rewrite(api: Api, scn: dict[str, Any]) -> None:
    r = scn["rewrite"]
    print(f'\nAct 6 (--live): a request, not an outcome, restated by gpt-oss-20b\n    {r["who"]}: "{r["query"]}"')
    if not os.environ.get("OPENROUTER_API_KEY"):
        check("rewrite act", False, "no OPENROUTER_API_KEY", warn=True)
        return
    from runtime.api import agent_search
    t = time.monotonic()
    restated = agent_search.expand_query(r["query"])[len(r["query"]):].strip()
    print(f"        restated in {time.monotonic() - t:.1f}s: {restated[:140]}")
    hits = api.search(r["who"], r["query"], rewrite="true")
    show(hits)
    check("the model restated the request as an outcome", bool(restated))
    check(f'the rewritten search still finds {r["expect_first"]}', names(hits)[:1] == [r["expect_first"]])


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="run act 6 with the real model (~$0.0002)")
    args = ap.parse_args()

    from runtime.engine.rehearse import _load_env
    _load_env()
    os.environ.pop("DATABASE_URL", None)          # the files catalog: same answers on every laptop
    os.environ["REAGENT_LAYERS"] = "files"
    os.environ["REAGENT_MODEL"] = "live" if args.live else "fake"
    os.environ.setdefault("CONNECTOR_SHARED_SECRET", "search-demo-" + secrets.token_hex(8))

    import logging
    for noisy in ("httpx", "mcp", "mcp.server", "mcp.client", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import yaml
    scn = yaml.safe_load(SCENARIO.read_text())
    base, server = serve()
    print(f"runtime app at {base}  catalog=spec/examples  db=none  model={os.environ['REAGENT_MODEL']}")
    api = Api(base)
    try:
        act_search(api, scn)
        act_wording(api, scn)
        act_isolation(api, scn)
        act_tie_break(api, scn)
        act_platform(base, scn)
        if args.live:
            act_rewrite(api, scn)
        act_fork(api, scn)        # last: the only act that adds to the catalog
    finally:
        server.should_exit = True

    bad = [n for lvl, n, _ in results if lvl == "FAIL"]
    warn = sum(1 for lvl, _, _ in results if lvl == "WARN")
    print(f"\n{sum(1 for lvl, _, _ in results if lvl == 'PASS')}/{len(results)} passed"
          + (f", {warn} warning(s)" if warn else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
