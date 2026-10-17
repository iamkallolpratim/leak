"use client";
import Link from "next/link";
import { Suspense, useState } from "react";
import { useParam } from "@/components/use-param";
import { api, useData } from "@/lib/api";

export default function LlmPage() {
  return <Suspense><Payloads /></Suspense>;
}

function Payloads() {
  const id = useParam("id");
  const runs = useData(api.runs);
  const runId = id ?? runs.data?.[0]?.run_id ?? null;
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">What the LLM saw</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          The exact system prompt and messages sent to the model on each step, as recorded by the harness
          <i> after</i> the privacy guard ran. Personal details appear only as tokens like <code>[CARD_1]</code>;
          the originals never left this machine.
        </p>
      </div>
      {runs.data && runs.data.length > 1 && (
        <div className="flex flex-wrap gap-2 text-xs">
          {runs.data.slice(0, 8).map((r) => (
            <Link key={r.run_id} href={`/llm/?id=${r.run_id}`}
              className={`rounded-md border px-2 py-1 font-mono ${r.run_id === runId ? "bg-muted" : ""}`}>{r.run_id}</Link>
          ))}
        </div>
      )}
      {runId ? <RunPayloads id={runId} /> : <p className="text-sm text-muted-foreground">No agent runs yet.</p>}
    </div>
  );
}

function RunPayloads({ id }: { id: string }) {
  const { data, error } = useData(() => api.payloads(id), [id]);
  const [open, setOpen] = useState<number | null>(0);
  if (error) return <p className="text-sm text-destructive">{error}</p>;
  return (
    <ol className="flex flex-col gap-3">
      {data?.map((p, i) => (
        <li key={i} className="rounded-lg border bg-card">
          <button className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm" onClick={() => setOpen(open === i ? null : i)}>
            <b>Step {p.step}</b>
            <span className="text-muted-foreground">{p.messages.length} messages · tools: {p.tools.length}</span>
            <span className="ml-auto text-xs text-muted-foreground">{open === i ? "hide" : "show"}</span>
          </button>
          {open === i && (
            <div className="flex flex-col gap-3 border-t p-4">
              <Block label="system" text={p.system} />
              {p.messages.map((m, j) => (
                <Block key={j} label={m.role}
                  text={[m.text, ...m.tool_calls.map((c: any) => `→ ${c.name}(${JSON.stringify(c.args)})`),
                    ...m.tool_results.map((r: any) => `← ${r.name}: ${typeof r.content === "string" ? r.content : JSON.stringify(r.content, null, 2)}`)]
                    .filter(Boolean).join("\n")} />
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Block({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-mono text-xs leading-relaxed">{text}</pre>
    </div>
  );
}
