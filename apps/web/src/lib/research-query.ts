import { opportunitySorts, opportunityStatuses, type OpportunitySort, type OpportunityStatus } from "@/lib/opportunity-api";

export type SearchParams = Record<string, string | string[] | undefined>;

export function singleValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export function positiveInteger(value: string | string[] | undefined, fallback: number, maximum = 100): number {
  const selected = singleValue(value);
  if (!selected || !/^\d+$/.test(selected)) return fallback;
  const parsed = Number.parseInt(selected, 10);
  return parsed >= 1 && parsed <= maximum ? parsed : fallback;
}

export function nonNegativeInteger(value: string | string[] | undefined, fallback = 0): number {
  const selected = singleValue(value);
  if (!selected || !/^\d+$/.test(selected)) return fallback;
  return Number.parseInt(selected, 10);
}

export function opportunityStatus(value: string | string[] | undefined): OpportunityStatus | undefined {
  const selected = singleValue(value);
  return opportunityStatuses.find((candidate) => candidate === selected);
}

export function opportunitySort(value: string | string[] | undefined): OpportunitySort {
  const selected = singleValue(value);
  return opportunitySorts.find((candidate) => candidate === selected) ?? "knowledge_cutoff_desc";
}

export function hrefWithQuery(
  pathname: string,
  current: SearchParams,
  updates: Record<string, string | number | null | undefined>
): string {
  const params = new URLSearchParams();
  for (const [key, raw] of Object.entries(current)) {
    const value = singleValue(raw);
    if (value !== undefined && value !== "") params.set(key, value);
  }
  for (const [key, value] of Object.entries(updates)) {
    if (value === null || value === undefined || value === "") params.delete(key);
    else params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}
