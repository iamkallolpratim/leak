import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium", {
  variants: {
    variant: {
      default: "border-transparent bg-muted text-foreground",
      cancel: "border-transparent bg-destructive/15 text-destructive",
      dispute: "border-transparent bg-destructive/15 text-destructive",
      downgrade: "border-transparent bg-warning/20 text-foreground",
      review: "border-transparent bg-info/15 text-info",
      keep: "border-transparent bg-muted text-muted-foreground",
      outline: "text-muted-foreground",
    },
  },
  defaultVariants: { variant: "default" },
});

export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
