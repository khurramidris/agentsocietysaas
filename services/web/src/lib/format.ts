import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";

dayjs.extend(relativeTime);

export function fromNow(value: string | number | null | undefined): string {
  if (value == null || value === "") return "never";
  const d = typeof value === "number" ? dayjs.unix(value) : dayjs(value);
  return d.isValid() ? d.fromNow() : "—";
}

export function dateTime(value: string | number | null | undefined): string {
  if (value == null || value === "") return "—";
  const d = typeof value === "number" ? dayjs.unix(value) : dayjs(value);
  return d.isValid() ? d.format("YYYY-MM-DD HH:mm") : "—";
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function usd(n: number | null | undefined, digits = 2): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  const d = abs > 0 && abs < 0.01 ? 4 : digits;
  return `${n < 0 ? "-" : ""}$${abs.toFixed(d)}`;
}

export function tokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
