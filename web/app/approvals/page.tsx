"use client";
import { CalendarPlus, Check, ExternalLink, Mail, X } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { api, useData, type Action } from "@/lib/api";
import { rupees, typeLabel } from "@/lib/utils";

export default function Approvals() {
  const { data, error, reload } = useData(api.actions);
  const open = data?.filter((a) => a.status === "pending" || a.status === "failed") ?? [];
  const done = data?.filter((a) => !(a.status === "pending" || a.status === "failed")) ?? [];
  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-semibold">Approvals</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Leak never acts on its own. Approving a draft creates it in your Gmail <b>Drafts</b> folder — it is not
          sent. Approving a reminder saves an .ics file for your calendar.
        </p>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {data && open.length === 0 && (
        <p className="rounded-lg border p-6 text-sm text-muted-foreground">Nothing waiting for approval.</p>
      )}
      <div className="flex flex-col gap-4">
        {open.map((a) => <PendingCard key={a.id} a={a} onDone={reload} />)}
      </div>
      {done.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="font-semibold">History</h2>
          <ul className="divide-y rounded-lg border bg-card text-sm">
            {done.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-2 px-4 py-3">
                <Badge variant={a.status === "executed" ? "review" : "keep"}>{a.status}</Badge>
                <span className="font-medium">{a.finding?.merchant ?? "?"}</span>
                <span className="text-muted-foreground">{a.tool === "draft_cancellation_email" ? "email draft" : "reminder"}</span>
                {a.result && <span className="w-full text-xs text-muted-foreground sm:ml-auto sm:w-auto">{a.result}</span>}
                {a.status === "executed" && a.tool === "create_renewal_reminder" && a.args.finding_id && (
                  <a className="text-xs text-primary underline" href={`/api/reminders/${a.args.finding_id}.ics`}>download .ics</a>
                )}
                {a.status === "executed" && a.tool === "draft_cancellation_email" && (
                  <a className="inline-flex items-center gap-1 text-xs text-primary underline" target="_blank" rel="noreferrer"
                    href="https://mail.google.com/mail/u/0/#drafts">open Gmail Drafts <ExternalLink className="size-3" /></a>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function PendingCard({ a, onDone }: { a: Action; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(a.status === "failed" ? a.result : null);
  const p = a.preview;
  const isDraft = a.tool === "draft_cancellation_email";

  async function run(kind: "approve" | "reject") {
    setBusy(true); setErr(null);
    try {
      const r = kind === "approve" ? await api.approve(a.id) : await api.reject(a.id);
      if (r.status === "failed") setErr(r.result);
      onDone();
    } catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground [&_svg]:size-4">
          {isDraft ? <Mail /> : <CalendarPlus />}
          {isDraft ? "Gmail draft" : "Calendar reminder"}
          <span>· requested by {a.run_id === "user" ? "you" : "the auditor agent"}</span>
          {a.status === "failed" && <Badge variant="cancel">failed — fix and retry</Badge>}
        </div>
        <CardTitle className="text-base">
          {a.finding?.merchant} · {typeLabel[a.finding?.type ?? ""] ?? a.finding?.type} ·{" "}
          <span className="tabular">{rupees(a.finding?.annual_impact_inr)}/yr</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {p?.kind === "email" && (
          <div className="rounded-md border bg-background text-sm">
            <div className="border-b px-4 py-2"><span className="text-muted-foreground">To: </span>{p.to || <i className="text-muted-foreground">you fill this in</i>}</div>
            <div className="border-b px-4 py-2"><span className="text-muted-foreground">Subject: </span>{p.subject}</div>
            <pre className="whitespace-pre-wrap p-4 font-sans leading-relaxed">{p.body}</pre>
          </div>
        )}
        {p?.kind === "email" && p.cancel_url && (
          <a className="inline-flex items-center gap-1 text-sm text-primary underline" href={p.cancel_url} target="_blank" rel="noreferrer">
            Or cancel directly on the website <ExternalLink className="size-3" />
          </a>
        )}
        {p?.kind === "ics" && <p className="text-sm">Reminder on <b>{p.date}</b>, with an alert 3 days before.</p>}
        {p?.kind === "error" && <p className="text-sm text-destructive">{p.error}</p>}
        {err && <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">{err}</p>}
      </CardContent>
      <CardFooter>
        <Button disabled={busy || p?.kind === "error"} onClick={() => run("approve")}>
          <Check />{isDraft ? "Approve — create Gmail draft" : "Approve — save reminder"}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => run("reject")}><X />Reject</Button>
      </CardFooter>
    </Card>
  );
}
