"use client";
import { useSearchParams } from "next/navigation";

// Static export can't pre-render dynamic routes, so detail pages use ?id=.
export function useParam(name: string) {
  return useSearchParams().get(name);
}
