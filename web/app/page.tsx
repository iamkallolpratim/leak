"use client";
import Link from "next/link";
import { FindingCard } from "@/components/finding-card";
import { ShareCardSheet } from "@/components/share-card";
import { Button } from "@/components/ui/button";
import { api, useData } from "@/lib/api";
import { rupees } from "@/lib/utils";

export default function Dashboard() {
  const summary = useData(api.summary);
  const findings = useData(api.findings);
  const reload = () => { summary.reload(); findings.reload(); };
  const error = summary.error ?? findings.error;

  const actionable = findings.data?.filter((f) => ["cancel", "dispute", "downgrade"].includes(f.suggested_action)) ?? [];
  const leaks = findings.data?.filter((f) => f.suggested_action === "review" && f.type !== "recurring") ?? [];
  const recurring = findings.data?.filter((f) => f.type === "recurring" || f.suggested_action === "keep") ?? [];

  return (
    <div className="flex flex-col gap-10">
      <section className="flex flex-col gap-4 rounded-lg border bg-card p-6 sm:p-8">
        <p className="text-sm font-medium text-muted-foreground">Potential savings found</p>
        <div className="flex flex-wrap items-end gap-x-3 gap-y-1">
          <span className="text-5xl font-semibold tracking-tight text-primary tabular sm:text-6xl">
            {summary.data ? rupees(summary.data.potential_savings_inr) : "…"}
          </span>
          <span className="pb-2 text-lg text-muted-foreground">/ year</span>
        </div>
        <p className="max-w-2xl text-sm text-muted-foreground">
          From things you can cancel, downgrade or dispute — each one backed by statement lines, emails or SMS
          you can inspect. Counted once per merchant.
        </p>
        {summary.data && <div><ShareCardSheet /></div>}
        {summary.data && (
          <dl className="grid grid-cols-2 gap-4 border-t pt-4 text-sm sm:grid-cols-4">
            <Stat label="More leaks to review" value={rupees(summary.data.review_inr) + "/yr"} />
            <Stat label="Recurring spend found" value={rupees(summary.data.recurring_inr) + "/yr"} />
            <Stat label="Findings" value={String(summary.data.findings)} />
            <Stat label="Awaiting your approval"
              value={<Link href="/approvals/" className="text-primary underline-offset-4 hover:underline">{summary.data.pending_actions}</Link>} />
          </dl>
        )}
      </section>

      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Couldn&apos;t reach the Leak API: {error}. Is <code>leak serve</code> running?
        </div>
      )}

      <Group title="Act on these" hint="Cancel, downgrade or dispute" items={actionable} reload={reload} />
      <Group title="Worth a look" hint="Price hikes, trials that converted, forex fees" items={leaks} reload={reload} />
      <Group title="Your subscriptions" hint="Recurring charges Leak found — keep what you use" items={recurring} reload={reload} />

      {findings.data?.length === 0 && (
        <div className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          No findings yet. Run <code>leak scan</code>, <code>leak extract</code> and <code>leak report</code>, then
          <Button variant="link" onClick={reload}>refresh</Button>.
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-lg font-medium tabular">{value}</dd>
    </div>
  );
}

function Group({ title, hint, items, reload }: {
  title: string; hint: string; items: import("@/lib/api").Finding[]; reload: () => void;
}) {
  if (!items.length) return null;
  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-baseline gap-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        <span className="text-sm text-muted-foreground">{hint}</span>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((f) => <FindingCard key={f.id} f={f} onChange={reload} />)}
      </div>
    </section>
  );
}
