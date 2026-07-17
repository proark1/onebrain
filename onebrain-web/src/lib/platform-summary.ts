export type LoadState = "loading" | "ready" | "error";

export function summaryValue(state: LoadState, count: number): number | "—" {
  return state === "ready" ? count : "—";
}
