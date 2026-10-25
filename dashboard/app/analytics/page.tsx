"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { listAgents, metrics, type AgentSummary, type Metrics } from "../../lib/api";
import { useDemoUser, useLive } from "../../lib/live";
import { Failure, PageHead, Skeleton, SourceTag, pct, usd } from "../ui";

const WINDOWS = [7, 30, 90] as const;

export default function AnalyticsPage() {
  const user = useDemoUser();
  const [agent, setAgent] = useState("");
  const [days, setDays] = useState<(typeof WINDOWS)[number]>(30);
  // Whole UTC days, so the key is stable across renders and the API buckets cleanly.
  const window = useMemo(() => {
    const to = new Date(); to.setUTCHours(24, 0, 0, 0);
    const from = new Date(to.getTime() - days * 86400000);
    return { from: from.toISOString(), to: to.toISOString() };
  }, [days]);
  const [agents] = useLive<{ agents: AgentSummary[] }>(user && `agents:${user}`, () => listAgents());
  const [state, reload] = useLive<Metrics>(user && `metrics:${user}:${agent}:${days}`, () => metrics({ ...window, ...(agent ? { agent } : {}) }));
  const data = state.phase === "ready" ? state.data : null;
  const daily = (data?.runs_over_time ?? []).slice().reverse();
  const peak = Math.max(1, ...daily.map(row => row.runs ?? 0));
  const tools = data?.top_tools ?? [];
  const evals = data?.eval_pass_rate_by_version ?? [];
  const spent = data?.cost_usd ?? 0;

  return <>
    <PageHead crumb="Runs / Analytics" title="Analytics" subtitle="Computed from stored runs and trace events, restricted on the server to what this identity can see.">
      <SourceTag state={state} />
      <select className="select" aria-label="Agent" value={agent} onChange={event => setAgent(event.target.value)}>
        <option value="">All visible agents</option>
        {agents.phase === "ready" && agents.data.agents.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
      </select>
      <div className="segmented" role="tablist" aria-label="Time window">
        {WINDOWS.map(value => <button key={value} type="button" role="tab" aria-selected={days === value} className={days === value ? "is-on" : undefined} onClick={() => setDays(value)}>{value}d</button>)}
      </div>
      <button className="button" type="button" onClick={reload}>Refresh</button>
    </PageHead>

    {state.phase === "loading" && <><Skeleton rows={1} columns={6} label="Loading totals" /><Skeleton rows={8} columns={4} label="Loading daily runs" /></>}
    <Failure state={state} notFound={<>No analytics are visible to this identity for that filter.</>} />
    {data && <>
      <div className="stat-row">
        <div><span>Success rate</span><b>{pct(data.success_rate)}</b></div>
        <div><span>Cost</span><b>{usd(spent)}</b>{data.budget_usd != null && <em className={spent > data.budget_usd ? "denied-text" : "muted"}>of {usd(data.budget_usd)} monthly cap</em>}</div>
        <div><span>p50 / p95 latency</span><b>{data.p50_latency_ms ?? 0} / {data.p95_latency_ms ?? 0} ms</b></div>
        <div><span>Human intervention</span><b>{pct(data.human_intervention_rate)}</b></div>
        <div><span>Denied tool calls</span><b className={data.denials ? "denied-text" : undefined}>{data.denials ?? 0}</b></div>
        <div><span>Policy violations</span><b className={data.policy_violations ? "denied-text" : undefined}>{data.policy_violations ?? 0}</b></div>
      </div>

      <section className="table metrics-table" aria-label="Runs per day">
        <div className="table-head"><span>Day (UTC)</span><span>Runs</span><span>Failures</span><span>Cost</span><span>Volume</span></div>
        {daily.map(row => <div className="table-row" key={row.bucket}>
          <span className="mono">{row.bucket?.slice(0, 10)}</span>
          <span className="num">{row.runs ?? 0}</span>
          <span className={`num ${row.failures ? "denied-text" : ""}`}>{row.failures ?? 0}</span>
          <span className="num">{usd(row.cost_usd)}</span>
          <span className="trace-bar" aria-hidden="true">
            <i className="bar" style={{ left: 0, width: `${((row.runs ?? 0) / peak) * 100}%` }} />
            {row.failures ? <i className="bar bar-denied" style={{ left: 0, width: `${(row.failures / peak) * 100}%` }} /> : null}
          </span>
        </div>)}
        {!daily.length && <div className="empty">No runs in this window.</div>}
      </section>

      <div className="split">
        <section className="table compact-table metrics-tools" aria-label="Top tools">
          <div className="table-head"><span>Action</span><span>Calls</span><span>Denied</span></div>
          {tools.map(tool => <div className="table-row" key={tool.action}>
            <code>{tool.action}</code>
            <span className="num">{tool.calls ?? 0}</span>
            <span className={`num ${tool.denials ? "denied-text" : ""}`}>{tool.denials ?? 0}</span>
          </div>)}
          {!tools.length && <div className="empty">No tool calls in this window.</div>}
        </section>
        <section className="table compact-table metrics-evals" aria-label="Eval pass rate by version">
          <div className="table-head"><span>Version</span><span>CI pass rate</span></div>
          {evals.map(row => <div className="table-row" key={row.version}>
            <Link className="mono" href={`/?agent=${row.version?.split("@")[0]}`}>{row.version}</Link>
            <span className="num">{pct(row.pass_rate)}</span>
          </div>)}
          {!evals.length && <div className="empty">No eval results in this window.</div>}
        </section>
      </div>
    </>}
  </>;
}
