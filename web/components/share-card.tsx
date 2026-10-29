"use client";
import { Check, Copy, Download, Share2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { api, type ShareCard } from "@/lib/api";

// The card is drawn by the local API (leak/share.py) from totals and catalog
// merchant names only; here we just preview it, turn it into a PNG in the
// browser, and hand it to the OS share sheet. Nothing is uploaded by Leak:
// the X / WhatsApp buttons only open a compose window with the share text.

const SIZE = { w: 1080, h: 1350 };

async function toPng(svgUrl: string): Promise<Blob> {
  const svg = await (await fetch(svgUrl, { cache: "no-store" })).text();
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = SIZE.w;
    canvas.height = SIZE.h;
    canvas.getContext("2d")!.drawImage(img, 0, 0, SIZE.w, SIZE.h);
    return await new Promise((ok, fail) =>
      canvas.toBlob((b) => (b ? ok(b) : fail(new Error("PNG export failed"))), "image/png"));
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function ShareCardSheet() {
  const [open, setOpen] = useState(false);
  const [names, setNames] = useState(true);
  const [card, setCard] = useState<ShareCard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const svgUrl = api.shareCardSvg(names);

  useEffect(() => {
    if (!open) return;
    setError(null);
    api.shareCard(names).then(setCard, (e) => setError(String(e.message)));
  }, [open, names]);

  async function run(fn: () => Promise<void>) {
    setBusy(true); setError(null);
    try { await fn(); }
    catch (e) { if ((e as Error).name !== "AbortError") setError(String((e as Error).message)); }
    finally { setBusy(false); }
  }

  const download = () => run(async () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await toPng(svgUrl));
    a.download = "leak-card.png";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  const nativeShare = () => run(async () => {
    const file = new File([await toPng(svgUrl)], "leak-card.png", { type: "image/png" });
    const data = { files: [file], text: card?.text ?? "" };
    if (navigator.canShare?.(data)) await navigator.share(data);
    else throw new Error("This browser can't share images directly. Download the PNG instead.");
  });

  const copy = () => run(async () => {
    await navigator.clipboard.writeText(card?.text ?? "");
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  });

  const text = encodeURIComponent(card?.text ?? "");
  const canNativeShare = typeof navigator !== "undefined" && "canShare" in navigator;

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button><Share2 />Share my Leak Card</Button>
      </SheetTrigger>
      <SheetContent title="Your Leak Card"
        description="Made on this machine from totals and well-known merchant names only. No accounts, dates, emails or amounts per charge.">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img key={svgUrl} src={svgUrl} alt={card?.text ?? "Leak Card"} width={SIZE.w} height={SIZE.h}
          className="h-auto w-full rounded-lg border" />

        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={names} onChange={(e) => setNames(e.target.checked)} className="size-4 accent-[var(--primary)]" />
          Show merchant names (Netflix, Amazon Prime…)
        </label>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <div className="flex flex-wrap gap-2">
          <Button onClick={download} disabled={busy || !card}><Download />Download PNG</Button>
          {canNativeShare && <Button variant="outline" onClick={nativeShare} disabled={busy || !card}><Share2 />Share…</Button>}
          <Button variant="outline" onClick={copy} disabled={busy || !card}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy text"}</Button>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <a href={`https://x.com/intent/post?text=${text}`} target="_blank" rel="noopener noreferrer">Post on X</a>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <a href={`https://wa.me/?text=${text}`} target="_blank" rel="noopener noreferrer">WhatsApp</a>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <a href={`https://www.linkedin.com/feed/?shareActive=true&text=${text}`} target="_blank" rel="noopener noreferrer">LinkedIn</a>
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Those buttons open a compose window with the text below; attach the downloaded PNG yourself. Leak never posts for you.
        </p>
        {card && <p className="rounded-md bg-muted p-3 text-sm">{card.text}</p>}
      </SheetContent>
    </Sheet>
  );
}
