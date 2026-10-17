"use client";
import Link from "next/link";
import { Suspense } from "react";
import { useParam } from "@/components/use-param";
import { Badge } from "@/components/ui/badge";
import { api, useData, type TraceEvent } from "@/lib/api";

export default function RunsPage() {
  return <Suspense><Runs /></Suspense>;
}

function Runs() {
  const id = useParam("id");
  return id ? <RunDetail id={id} /> : <RunList />;
}

function RunList() {
  const { data, error } = useData(api.runs);
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Agent runs</h1>
        <p className="mt-1 text-sm text-muted-foreground">Every step the harness took, from the JSONL trace. Same as <code>leak trace replay</code>.</p>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <ul className="divide-y rounded-lg border bg-card">
        {data?.map((r) => (
          <li key={r.run_id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
            <Link href={`/runs/?id=${r.run_id}`} className="font-mono text-primary underline-offset-4 hover:underline">{r.run_id}</Link>
            <Badge variant={r.status === "completed" ? "review" : "cancel"}>{r.status}</Badge>
            <span className="text-muted-foreground">{r.model}</span>
            <span className="text-muted-foreground">{r.started?.slice(0, 16).replace("T", " ")}</span>
            {r.budget && <span className="ml-auto tabular text-muted-foreground">{r.budget.steps} steps · ${Number(r.budget.spent_usd).toFixed(3)} · ₹{Number(r.budget.spent_inr).toFixed(2)}</span>}
            <Link href={`/llm/?id=${r.run_id}`} className="text-xs text-primary underline">payloads</Link>
          </li>
        ))}
        {data?.length === 0 && <li className="px-4 py-6 text-sm text-muted-foreground">No runs yet — run <code>leak report</code>.</li>}
      </ul>
    </div>
  );
}

const short = (v: unknown, n = 220) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};

function RunDetail({ id }: { id: string }) {
  const { data, error } = useData(() => api.run(id), [id]);
  const steps = new Map<number, TraceEvent[]>();
  data?.forEach((e) => steps.set(e.step, [...(steps.get(e.step) ?? []), e]));
  const end = data?.find((e) => e.type === "run_end");
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <Link href="/runs/" className="text-sm text-muted-foreground hover:underline">← runs</Link>
        <h1 className="font-mono text-xl font-semibold">{id}</h1>
        <Link href={`/llm/?id=${id}`} className="text-sm text-primary underline">What the LLM saw →</Link>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {end && (
        <div className="rounded-lg border bg-card p-4 text-sm">
          <b>{end.data.status}</b> {end.data.reason} · {end.data.budget.steps} steps · {end.data.budget.input_tokens} in / {end.data.budget.output_tokens} out tokens · ${end.data.budget.spent_usd} (₹{end.data.budget.spent_inr})
          {end.data.final_text && <p className="mt-3 whitespace-pre-wrap text-muted-foreground">{end.data.final_text}</p>}
        </div>
      )}
      <ol className="flex flex-col gap-3">
        {[...steps.entries()].filter(([s]) => s > 0).map(([step, evs]) => (
          <li key={step} className="rounded-lg border bg-card">
            <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">Step {step}</div>
            <ul className="flex flex-col gap-1.5 p-4 font-mono text-xs">
              {evs.map((e) => <EventLine key={e.seq} e={e} />)}
            </ul>
          </li>
        ))}
      </ol>
    </div>
  );
}

function EventLine({ e }: { e: TraceEvent }) {
  const d = e.data;
  switch (e.type) {
    case "llm_response":
      return (
        <li className="text-muted-foreground">
          model · {d.response.usage.input_tokens} in / {d.response.usage.output_tokens} out · ${d.cost_usd}
          {d.response.text && <div className="mt-1 whitespace-pre-wrap font-sans text-sm text-foreground">{short(d.response.text, 600)}</div>}
        </li>
      );
    case "tool_call":
      return <li>→ <b>{d.name}</b>({short(d.args, 160)}) <span className="text-muted-foreground">[{d.permission}]</span></li>;
    case "tool_result":
      return <li className={d.is_error ? "text-destructive" : "text-muted-foreground"}>{d.is_error ? "✗" : "←"} {short(d.content)}</li>;
    case "approval_queued":
      return <li className="text-info">⏸ queued for approval as {d.action_id} — not executed</li>;
    default:
      return null;
  }
}
