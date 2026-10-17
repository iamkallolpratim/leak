"use client";
import { Eye, EyeOff, FileText, Mail, MessageSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { api, type Finding, type FindingDetail } from "@/lib/api";

const icon = (id: string) =>
  id.startsWith("gmail:") ? <Mail /> : id.startsWith("sms:") ? <MessageSquare /> : <FileText />;

export function EvidenceSheet({ finding }: { finding: Finding }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<FindingDetail | null>(null);
  const [unmasked, setUnmasked] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open && !detail) api.finding(finding.id).then(setDetail, (e) => setError(String(e.message)));
  }, [open, detail, finding.id]);

  async function toggle(eid: string) {
    if (unmasked[eid]) {
      const { [eid]: _, ...rest } = unmasked;
      setUnmasked(rest);
      return;
    }
    const r = await api.unmask(eid);
    setUnmasked((u) => ({ ...u, [eid]: r.text }));
  }

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm"><FileText />Evidence ({finding.evidence.length})</Button>
      </SheetTrigger>
      <SheetContent title={`Evidence · ${finding.merchant}`}
        description="Exact source lines behind this finding. Masked by default; unmasking happens on this machine only.">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!detail && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
        <ol className="flex flex-col gap-3">
          {detail?.evidence_detail.map((e) => (
            <li key={e.evidence_id} className="rounded-md border bg-card">
              <div className="flex items-center gap-2 border-b px-3 py-2 text-xs text-muted-foreground [&_svg]:size-3.5">
                {icon(e.evidence_id)}
                <code className="truncate">{e.evidence_id}</code>
                {e.note && <span className="truncate">· {e.note}</span>}
                <Button variant="ghost" size="sm" className="ml-auto h-6 px-1.5" onClick={() => toggle(e.evidence_id)}
                  title={unmasked[e.evidence_id] ? "Hide personal details" : "Show personal details (local)"}>
                  {unmasked[e.evidence_id] ? <EyeOff /> : <Eye />}
                </Button>
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed">
                {unmasked[e.evidence_id] ?? e.text ?? "(missing)"}
              </pre>
            </li>
          ))}
        </ol>
      </SheetContent>
    </Sheet>
  );
}
