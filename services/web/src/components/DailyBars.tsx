import * as React from "react";

// Single-series column chart for a value per day (for example daily spend).
// Marks follow the dataviz specs: columns at most 24px wide with a 4px rounded
// data end, square at the baseline; hairline gridlines; muted axis text; one
// tooltip per column on hover and keyboard focus; the peak is labelled directly.

const C = {
  surface: "#fcfcfb",
  series: "#2a78d6",
  seriesHover: "#5598e7",
  grid: "#e1e0d9",
  baseline: "#c3c2b7",
  muted: "#898781",
  ink: "#0b0b0b",
  ink2: "#52514e",
};

export interface DayValue {
  date: string; // YYYY-MM-DD
  value: number;
}

function niceStep(max: number, ticks: number): number {
  const raw = max / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
}

function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function DailyBars({ data, format, height = 220, label }: { data: DayValue[]; format: (v: number) => string; height?: number; label: string }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(720);
  const [active, setActive] = React.useState<number | null>(null);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(240, entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pad = { top: 22, right: 8, bottom: 26, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(0, ...data.map((d) => d.value));
  const step = max > 0 ? niceStep(max, 4) : 1;
  const top = max > 0 ? Math.ceil(max / step) * step : 1;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const band = data.length ? plotW / data.length : plotW;
  const barW = Math.max(2, Math.min(24, band - 2));
  const y = (v: number) => pad.top + plotH - (v / top) * plotH;
  const peak = data.reduce((best, d, i) => (d.value > (data[best]?.value ?? -1) ? i : best), 0);
  const labelEvery = Math.max(1, Math.ceil(data.length / Math.max(2, Math.floor(plotW / 64))));

  const bar = (v: number, x: number) => {
    const h = Math.max(0, (v / top) * plotH);
    if (h <= 0) return "";
    const r = Math.min(4, h, barW / 2);
    const x0 = x - barW / 2;
    const yTop = pad.top + plotH - h;
    const yBase = pad.top + plotH;
    return `M${x0},${yBase}V${yTop + r}Q${x0},${yTop} ${x0 + r},${yTop}H${x0 + barW - r}Q${x0 + barW},${yTop} ${x0 + barW},${yTop + r}V${yBase}Z`;
  };

  const tip = active !== null ? data[active] : null;
  const tipX = active !== null ? pad.left + band * (active + 0.5) : 0;

  return (
    <div ref={ref} style={{ position: "relative", width: "100%" }}>
      <svg width={width} height={height} role="img" aria-label={label} style={{ display: "block", background: C.surface, borderRadius: 8 }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} stroke={t === 0 ? C.baseline : C.grid} strokeWidth={1} shapeRendering="crispEdges" />
            <text x={pad.left - 8} y={y(t)} dy="0.32em" textAnchor="end" fontSize={11} fill={C.muted} style={{ fontVariantNumeric: "tabular-nums" }}>
              {format(t)}
            </text>
          </g>
        ))}
        {data.map((d, i) => {
          const cx = pad.left + band * (i + 0.5);
          return (
            <g
              key={d.date}
              tabIndex={0}
              role="listitem"
              aria-label={`${shortDate(d.date)}: ${format(d.value)}`}
              onPointerEnter={() => setActive(i)}
              onPointerLeave={() => setActive((a) => (a === i ? null : a))}
              onFocus={() => setActive(i)}
              onBlur={() => setActive((a) => (a === i ? null : a))}
              style={{ outline: "none", cursor: "default" }}
            >
              {/* The hit target is the whole column slot, not just the painted bar. */}
              <rect x={pad.left + band * i} y={pad.top} width={band} height={plotH} fill="transparent" />
              <path d={bar(d.value, cx)} fill={active === i ? C.seriesHover : C.series} />
              {i % labelEvery === 0 && (
                <text x={cx} y={height - 8} textAnchor="middle" fontSize={11} fill={C.muted}>
                  {shortDate(d.date)}
                </text>
              )}
            </g>
          );
        })}
        {max > 0 && active === null && (
          <text x={pad.left + band * (peak + 0.5)} y={y(data[peak].value) - 6} textAnchor="middle" fontSize={11} fontWeight={600} fill={C.ink2}>
            {format(data[peak].value)}
          </text>
        )}
      </svg>
      {tip && (
        <div
          style={{
            position: "absolute",
            left: Math.min(Math.max(tipX, 70), width - 70),
            top: Math.max(0, y(tip.value) - 56),
            transform: "translateX(-50%)",
            background: "#fff",
            border: "1px solid rgba(11,11,11,0.10)",
            boxShadow: "0 4px 14px rgba(0,0,0,0.08)",
            borderRadius: 6,
            padding: "6px 10px",
            pointerEvents: "none",
            whiteSpace: "nowrap",
            fontSize: 12,
          }}
        >
          <div style={{ color: C.ink, fontWeight: 600, fontSize: 13 }}>{format(tip.value)}</div>
          <div style={{ color: C.ink2, display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 10, height: 2, background: C.series, display: "inline-block" }} />
            {shortDate(tip.date)}
          </div>
        </div>
      )}
    </div>
  );
}

/** Fill the days without data with zeros so the time axis is continuous. */
export function fillDays(rows: { date: string; value: number }[], days: number, today = new Date()): DayValue[] {
  const byDate = new Map(rows.map((r) => [r.date.slice(0, 10), r.value]));
  const out: DayValue[] = [];
  const end = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  for (let i = days - 1; i >= 0; i -= 1) {
    const iso = new Date(end - i * 86_400_000).toISOString().slice(0, 10);
    out.push({ date: iso, value: byDate.get(iso) ?? 0 });
  }
  return out;
}
