"use client";
import { BellPlus, Check, MailPlus } from "lucide-react";
import { useState } from "react";
import { EvidenceSheet } from "@/components/evidence-sheet";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type Finding } from "@/lib/api";
import { rupees, typeLabel } from "@/lib/utils";

const DRAFT = "draft_cancellation_email";
const REMINDER = "create_renewal_reminder";

export function FindingCard({ f, onChange }: { f: Finding; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const pending = new Set(f.pending ?? []);

  async function queue(tool: string) {
    setBusy(tool); setErr(null);
    try { await api.requestAction(f.id, tool); onChange(); }
    catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(null); }
  }

  const canDraft = ["cancel", "dispute", "downgrade"].includes(f.suggested_action);
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={f.suggested_action}>{f.suggested_action}</Badge>
          <Badge variant="outline">{typeLabel[f.type] ?? f.type}</Badge>
          {f.source === "detector" && <Badge variant="outline" title="Not yet reviewed by the auditor agent">detector</Badge>}
          <span className="ml-auto text-xs text-muted-foreground tabular" title="confidence">{Math.round(f.confidence * 100)}%</span>
        </div>
        <CardTitle className="mt-1 text-base">{f.merchant}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-semibold tabular">{rupees(f.annual_impact_inr)}</span>
          <span className="text-sm text-muted-foreground">/ year</span>
          <span className="ml-auto text-sm text-muted-foreground tabular">
            {rupees(f.amount_inr)}{f.cadence && f.cadence !== "one_off" ? ` · ${f.cadence}` : ""}
          </span>
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">{f.explanation}</p>
        {f.next_renewal && <p className="text-xs text-muted-foreground">Next renewal: {f.next_renewal}</p>}
        {err && <p className="text-xs text-destructive">{err}</p>}
      </CardContent>
      <CardFooter>
        <EvidenceSheet finding={f} />
        {canDraft && (
          pending.has(DRAFT)
            ? <Badge variant="review"><Check className="mr-1 size-3" />Draft awaiting approval</Badge>
            : <Button size="sm" variant="outline" disabled={!!busy} onClick={() => queue(DRAFT)}><MailPlus />Draft email</Button>
        )}
        {f.next_renewal && (
          pending.has(REMINDER)
            ? <Badge variant="review"><Check className="mr-1 size-3" />Reminder queued</Badge>
            : <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => queue(REMINDER)}><BellPlus />Remind me</Button>
        )}
      </CardFooter>
    </Card>
  );
}
