import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./api-schema";

export type Rejection = components["schemas"]["Rejection"];
export type AgentSummary = components["schemas"]["AgentSummary"];
export type AgentDetail = components["schemas"]["AgentDetail"];
export type RunResult = components["schemas"]["RunResult"];
export type Approval = components["schemas"]["Approval"];
export type PublishRun = components["schemas"]["PublishRun"];
export type EvalReport = components["schemas"]["EvalReport"];
export type Metrics = components["schemas"]["Metrics"];
export type RunEvent = components["schemas"]["run_events.schema"];
export type Trace = { run_id: number; children?: number[]; events: RunEvent[] };

// The root .env names it NEXT_PUBLIC_API_URL; dashboard/.env.example names it NEXT_PUBLIC_REAGENT_API_URL.
export const API_URL = process.env.NEXT_PUBLIC_REAGENT_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// The three log-in-as identities. Roles are enforced server-side; this only picks the header.
export const DEMO_USERS = [
  { id: "alice", name: "Alice", role: "Approver", team: "platform-eng" },
  { id: "bob", name: "Bob", role: "Member", team: "platform-eng" },
  { id: "frank", name: "Frank", role: "Enterprise admin", team: null },
] as const;

const USER_KEY = "reagent-demo-user";
/** sessionStorage key for a freshly forked file, handed from the collection to the editor. */
export const DRAFT_KEY = (name: string) => `reagent-draft:${name}`;

export function getDemoUser(): string {
  try {
    if (typeof window !== "undefined") return window.sessionStorage.getItem(USER_KEY) ?? "alice";
  } catch { /* storage blocked: fall through to the default identity */ }
  return "alice";
}

export function setDemoUser(user: string) {
  try { window.sessionStorage.setItem(USER_KEY, user); } catch { /* keep going: reload still re-renders */ }
  window.location.reload();
}

// FROZEN D2: the acting principal comes only from this header.
const identity: Middleware = {
  onRequest({ request }) {
    request.headers.set("X-ReAgent-User", getDemoUser());
    return request;
  },
};

// Keep this client at module scope: Next dev hot reload must not allocate one per request.
export const api = createClient<paths>({ baseUrl: API_URL });
api.use(identity);

export type FetchResult = { data?: unknown; error?: unknown; response: Response };

export type Outcome<T> =
  | { ok: true; data: T; status: number }
  | { ok: false; status: number; detail: string; rejections: Rejection[]; unreachable: boolean; missingRoute: boolean };

/** Normalises an openapi-fetch call, including a refused connection, into one shape. */
export async function call<T>(request: Promise<FetchResult>): Promise<Outcome<T>> {
  try {
    const { data, error, response } = await request;
    if (response.ok) return { ok: true, data: data as T, status: response.status };
    const body = (error ?? {}) as { detail?: unknown; rejections?: Rejection[] };
    const detail = typeof body.detail === "string" ? body.detail : body.detail ? JSON.stringify(body.detail) : response.statusText;
    // FastAPI's own 404 for an unmounted route. The API's visibility 404 says "not found" in lower case.
    const missingRoute = response.status === 404 && body.detail === "Not Found";
    return { ok: false, status: response.status, detail, rejections: body.rejections ?? [], unreachable: false, missingRoute };
  } catch {
    return { ok: false, status: 0, detail: `The runtime API at ${API_URL} is not reachable.`, rejections: [], unreachable: true, missingRoute: false };
  }
}

export const listAgents = (q?: string) => api.GET("/v0/agents", { params: { query: q ? { q } : {} } });
export const agentDetail = (name: string) => api.GET("/v0/agents/{name}", { params: { path: { name } } });
export const resolvedAgent = (name: string) => api.GET("/v0/agents/{name}/resolved", { params: { path: { name } } });
export const validateAgent = (name: string, spec: string) => api.POST("/v0/agents/{name}/validate", { params: { path: { name } }, body: { spec } });
export const forkAgent = (name: string, new_name: string) => api.POST("/v0/agents/{name}/fork", { params: { path: { name } }, body: { new_name } });
export const startRun = (name: string, input: Record<string, unknown>) =>
  api.POST("/v0/agents/{name}/runs", { params: { path: { name } }, body: { input: input as Record<string, never>, wait: true } });
export const runResult = (id: number) => api.GET("/v0/runs/{id}", { params: { path: { id } } });
export const traceForRun = (id: number) => api.GET("/v0/runs/{id}/trace", { params: { path: { id } } });
// The runtime also accepts the draft text to publish; the contract does not declare that body, hence the cast.
export const startPublish = (name: string, spec?: string) =>
  api.POST("/v0/agents/{name}/publish", { params: { path: { name } }, ...(spec ? { body: { spec } as never } : {}) });
export const publishRun = (id: number) => api.GET("/v0/publish-runs/{id}", { params: { path: { id } } });
export const reviewCase = (id: number, caseId: string, decision: "approve" | "edit" | "reject", reason?: string, editedCase?: Record<string, unknown>) =>
  api.POST("/v0/publish-runs/{id}/cases/{case}/review", {
    params: { path: { id, case: caseId } },
    body: { decision, reason, edited_case: editedCase as Record<string, never> | undefined },
  });
export const dryRunCase = (id: number, caseId: string) => api.POST("/v0/publish-runs/{id}/cases/{case}/dry-run", { params: { path: { id, case: caseId } } });
export const continuePublish = (id: number) => api.POST("/v0/publish-runs/{id}/continue", { params: { path: { id } } });
export const listApprovals = (status: "pending" | "decided" | "all") => api.GET("/v0/approvals", { params: { query: { status } } });
export const decideApproval = (id: number, decision: "approved" | "rejected") => api.POST("/v0/approvals/{id}", { params: { path: { id } }, body: { decision } });
export const metrics = (query: { team?: string; agent?: string; from?: string; to?: string }) => api.GET("/v0/metrics", { params: { query } });
