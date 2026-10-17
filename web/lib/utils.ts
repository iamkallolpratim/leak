import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
export const rupees = (n: number | string | null | undefined) => (n == null ? "—" : inr.format(Number(n)));

export const typeLabel: Record<string, string> = {
  recurring: "Recurring",
  price_hike: "Price hike",
  trial_conversion: "Trial → paid",
  duplicate: "Duplicate",
  zombie: "Charged after cancelling",
  forex: "Forex fees",
  unused: "Unused",
};
