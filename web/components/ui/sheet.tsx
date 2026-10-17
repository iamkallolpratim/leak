"use client";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;

export function SheetContent({ className, children, title, description }: {
  className?: string; children: React.ReactNode; title: string; description?: string;
}) {
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
      <Dialog.Content
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col gap-4 overflow-y-auto border-l bg-background p-6 shadow-xl",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <Dialog.Title className="text-lg font-semibold">{title}</Dialog.Title>
            {description && <Dialog.Description className="text-sm text-muted-foreground">{description}</Dialog.Description>}
          </div>
          <Dialog.Close className="rounded-md p-1 hover:bg-muted" aria-label="Close">
            <X className="size-4" />
          </Dialog.Close>
        </div>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  );
}
