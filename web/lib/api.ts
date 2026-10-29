// Thin client for the local Leak API. Mutations carry the per-process token
// from /api/session; the server rejects anything without it.

export type Evidence = { evidence_id: string; note: string | null };
export type Finding = {
  id: string;
  type: string;
  merchant: string;
  merchant_id: string | null;
  amount_inr: number;
  cadence: string | null;
  annual_impact_inr: number;
  next_renewal: string | null;
  confidence: number;
  explanation: string;
  evidence: Evidence[];
  suggested_action: "cancel" | "downgrade" | "dispute" | "review" | "keep";
  source: "detector" | "agent";
  account: string | null;
  pending?: string[];
};
export type FindingDetail = Finding & {
  evidence_detail: { evidence_id: string; note: string | null; text: string | null }[];
};
export type Summary = {
  potential_savings_inr: number;
  review_inr: number;
  recurring_inr: number;
  findings: number;
  actionable: number;
  pending_actions: number;
};
export type ShareCard = {
  total_inr: number;
  kinds: string[];
  rows: { name: string; label: string; annual_inr: number }[];
  more: number;
  text: string;
};
export type Preview =
  | { kind: "email"; to: string; subject: string; body: string; cancel_url: string | null }
  | { kind: "ics"; ics: string; date: string }
  | { kind: "error"; error: string };
export type Action = {
  id: string;
  run_id: string;
  tool: string;
  args: { finding_id?: string };
  status: "pending" | "approved" | "rejected" | "executed" | "failed";
  created_at: string;
  result: string | null;
  finding: Finding | null;
  preview?: Preview;
};
export type Run = {
  run_id: string;
  started: string | null;
  model: string | null;
  prompt: string;
  status: string;
  budget: { steps: number; spent_usd: string; spent_inr: string; input_tokens: number; output_tokens: number } | null;
};
export type TraceEvent = { seq: number; ts: string; type: string; step: number; data: Record<string, any> };
export type Session = { token: string; demo: boolean; models: { extract: string; reason: string } };

let session: Promise<Session> | null = null;
export const getSession = () => (session ??= get<Session>("/api/session"));

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
  return r.json();
}

async function authed<T>(path: string, method: string, body?: unknown): Promise<T> {
  const { token } = await getSession();
  const r = await fetch(path, {
    method,
    headers: { "X-Leak-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
  return r.json();
}

export const api = {
  summary: () => get<Summary>("/api/summary"),
  findings: () => get<Finding[]>("/api/findings"),
  shareCard: (names: boolean) => get<ShareCard>(`/api/share-card?names=${names}`),
  shareCardSvg: (names: boolean) => `/api/share-card.svg?names=${names}`,
  finding: (id: string) => get<FindingDetail>(`/api/findings/${id}`),
  unmask: (eid: string) => authed<{ text: string }>(`/api/evidence/${eid}/unmasked`, "GET"),
  requestAction: (fid: string, tool: string) => authed<Action>(`/api/findings/${fid}/actions`, "POST", { tool }),
  actions: () => get<Action[]>("/api/actions"),
  approve: (id: string) => authed<Action>(`/api/actions/${id}/approve`, "POST"),
  reject: (id: string) => authed<Action>(`/api/actions/${id}/reject`, "POST"),
  runs: () => get<Run[]>("/api/runs"),
  run: (id: string) => get<TraceEvent[]>(`/api/runs/${id}`),
  payloads: (id: string) =>
    get<{ step: number; ts: string; system: string; messages: any[]; tools: string[] }[]>(`/api/runs/${id}/payloads`),
};

// Tiny data hook: load once, expose reload.
import { useCallback, useEffect, useState } from "react";
export function useData<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const reload = useCallback(() => load().then(setData, (e) => setError(String(e.message ?? e))), deps);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, reload };
}
