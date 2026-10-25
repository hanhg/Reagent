"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Editor, { type Monaco } from "@monaco-editor/react";
import { DRAFT_KEY, agentDetail, call, startPublish, validateAgent, type AgentDetail, type Rejection } from "../../../../lib/api";
import { recordedSpec } from "../../../../lib/fixtures";
import { useDemoUser, useLive } from "../../../../lib/live";
import { Failure, PageHead, Rejections, Skeleton, SourceTag, Verdict } from "../../../ui";

type Check = { phase: "idle" | "checking" } | { phase: "done"; valid: boolean; rejections: Rejection[] } | { phase: "error"; detail: string };
type EditorInstance = Parameters<NonNullable<React.ComponentProps<typeof Editor>["onMount"]>>[0];

/** Best-effort line for a rejection field such as `spec.budget.usd_per_run` or `spec.tools[1].actions`. */
function lineFor(yaml: string, field: string) {
  const lines = yaml.split("\n");
  const keys = field.split(".").map(key => key.replace(/\[\d+\]/g, "")).filter(key => key && !key.startsWith("("));
  let line = 0;
  let found = -1;
  for (const key of keys) {
    const pattern = new RegExp(`(^|[\\s{,-])${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:`);
    const next = lines.findIndex((text, index) => index >= line && pattern.test(text));
    if (next < 0) break;
    found = line = next;
  }
  return found + 1 || 1;
}

function readDraft(name: string) {
  try { return window.sessionStorage.getItem(DRAFT_KEY(name)) ?? undefined; } catch { return undefined; }
}

export default function EditorPage() {
  const params = useParams<{ name: string }>();
  const name = decodeURIComponent(params.name);
  const router = useRouter();
  const user = useDemoUser();
  const [state] = useLive<AgentDetail>(user && `agent:${user}:${name}`, () => agentDetail(name), {
    fallback: () => { const spec_yaml = readDraft(name) ?? recordedSpec(name); return spec_yaml ? { name, version: "", description: "", spec_yaml } : undefined; },
  });
  const [yaml, setYaml] = useState<string | null>(null);
  const [check, setCheck] = useState<Check>({ phase: "idle" });
  const [publishing, setPublishing] = useState(false);
  const [publishProblem, setPublishProblem] = useState<{ detail: string; rejections: Rejection[] } | null>(null);
  const editor = useRef<{ instance: EditorInstance; monaco: Monaco } | null>(null);

  useEffect(() => { if (state.phase === "ready" && yaml === null) setYaml(state.data.spec_yaml ?? ""); }, [state, yaml]);

  useEffect(() => {
    if (yaml === null) return;
    setCheck({ phase: "checking" });
    const timer = setTimeout(async () => {
      const outcome = await call<{ valid?: boolean; rejections?: Rejection[] }>(validateAgent(name, yaml));
      if (outcome.ok) setCheck({ phase: "done", valid: Boolean(outcome.data.valid), rejections: outcome.data.rejections ?? [] });
      else setCheck({ phase: "error", detail: outcome.detail });
    }, 500);
    return () => clearTimeout(timer);
  }, [name, yaml]);

  useEffect(() => {
    const current = editor.current;
    const model = current?.instance.getModel();
    if (!current || !model || yaml === null) return;
    const rejections = check.phase === "done" ? check.rejections : [];
    current.monaco.editor.setModelMarkers(model, "reagent", rejections.map(item => {
      const line = lineFor(yaml, item.field);
      return { startLineNumber: line, endLineNumber: line, startColumn: 1, endColumn: model.getLineMaxColumn(line), message: `${item.code} · ${item.message}`, severity: current.monaco.MarkerSeverity.Error };
    }));
  }, [check, yaml]);

  async function publish() {
    setPublishing(true); setPublishProblem(null);
    // Publish what is on screen once it resolves; otherwise the stored draft.
    const stored = state.phase === "ready" ? state.data.spec_yaml : undefined;
    const text = check.phase === "done" && check.valid && yaml && yaml !== stored ? yaml : undefined;
    const outcome = await call<{ publish_run_id: number }>(startPublish(name, text));
    setPublishing(false);
    if (outcome.ok) router.push(`/publish?run=${outcome.data.publish_run_id}`);
    else setPublishProblem({ detail: outcome.detail, rejections: outcome.rejections });
  }

  function beforeMount(monaco: Monaco) {
    const style = getComputedStyle(document.documentElement);
    const token = (name: string) => style.getPropertyValue(name).trim();
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    monaco.editor.defineTheme("reagent", { base: dark ? "vs-dark" : "vs", inherit: true, rules: [], colors: {
      "editor.background": token("--surface"), "editor.foreground": token("--ink"), "editorLineNumber.foreground": token("--muted"),
      "editorGutter.background": token("--surface"), "editor.lineHighlightBackground": token("--surface-2"), "editorError.foreground": token("--denied"),
    } });
  }

  const rejections = check.phase === "done" ? check.rejections : [];
  return <>
    <PageHead crumb="Agents / Editor" title={<span className="mono-title">{name}.agent</span>} subtitle="Validated against the live enterprise and team layers as you type. Nothing is saved from here.">
      <SourceTag state={state} />
      <Link className="button" href={`/?agent=${name}`}>Resolved view</Link>
      <button className="button primary" type="button" disabled={publishing} onClick={publish}>{publishing ? "Starting…" : "Start publish pipeline"}</button>
    </PageHead>
    {publishProblem && (publishProblem.rejections.length ? <Rejections items={publishProblem.rejections} title="Pipeline refused" /> : <div className="notice notice-denied">{publishProblem.detail}</div>)}
    {state.phase === "loading" && <Skeleton rows={14} columns={1} label="Loading agent file" />}
    <Failure state={state} notFound={<>{name} does not exist or is not visible to this identity.</>} />
    {state.phase === "ready" && yaml !== null && <div className="editor-layout">
      <div className="editor-frame">
        <Editor height="64vh" language="yaml" theme="reagent" beforeMount={beforeMount} onMount={(instance, monaco) => { editor.current = { instance, monaco }; }}
          loading={<Skeleton rows={14} columns={1} label="Loading editor" />} value={yaml} onChange={value => setYaml(value ?? "")}
          options={{ minimap: { enabled: false }, lineNumbers: "on", fontFamily: "IBM Plex Mono, monospace", fontSize: 13, lineHeight: 21, scrollBeyondLastLine: false, automaticLayout: true, renderLineHighlight: "line", padding: { top: 12 } }} />
      </div>
      <aside className="validation-panel" aria-live="polite">
        <div className="validation-head">
          <h2>Validation</h2>
          {check.phase === "checking" && <Verdict kind="pending">checking</Verdict>}
          {check.phase === "done" && (check.valid ? <Verdict kind="pass">resolves</Verdict> : <Verdict kind="fail">{rejections.length} rejected</Verdict>)}
        </div>
        {check.phase === "done" && check.valid && <p className="muted">Every value sits inside what the enterprise and team layers allow.</p>}
        {check.phase === "error" && <div className="notice notice-denied">{check.detail}</div>}
        {rejections.map((item, index) => <button type="button" className="validation-item" key={`${item.field}-${index}`} onClick={() => { const line = lineFor(yaml, item.field); editor.current?.instance.revealLineInCenter(line); editor.current?.instance.setPosition({ lineNumber: line, column: 1 }); editor.current?.instance.focus(); }}>
          <span className="rejection-meta"><code>{item.code}</code><span>line {lineFor(yaml, item.field)}</span><span>blocked by <b>{item.blocked_by}</b></span></span>
          <span>{item.message}</span>
        </button>)}
        <p className="panel-note">Publishing runs the pipeline on the text above when it resolves, and on the stored draft otherwise. Nothing is saved until the pipeline publishes.</p>
      </aside>
    </div>}
  </>;
}
