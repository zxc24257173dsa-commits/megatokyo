import { useEffect, useRef, useState, type ReactNode } from "react";

/** Width of a container, following resizes (charts are drawn in real pixels so text stays crisp). */
function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

const compact = (v: number) => (Math.abs(v) >= 10000 ? `${Math.round(v / 1000)}K` : v.toLocaleString("en-US"));

export interface Bar {
  label: string;
  value: number;
  tip: ReactNode;
}

/**
 * Single-series column chart: thin columns (<= 24px) with a 4px rounded top, square at the baseline,
 * hairline gridlines, one y-axis, a value on the latest column only, and a hover tooltip per column
 * (hit area = the whole slot). A single series needs no legend: the card title names it.
 */
export function ColumnChart({ bars, format, height = 220, ariaLabel }: { bars: Bar[]; format: (v: number) => string; height?: number; ariaLabel: string }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const padL = 44, padR = 8, padT = 18, padB = 26;
  const plotW = Math.max(0, width - padL - padR);
  const plotH = height - padT - padB;
  const max = niceMax(Math.max(0, ...bars.map((b) => b.value)));
  const slot = bars.length ? plotW / bars.length : 0;
  const barW = Math.max(2, Math.min(24, slot - 6));
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const ticks = [0, max / 2, max];
  const every = Math.max(1, Math.ceil(bars.length / Math.max(1, Math.floor(plotW / 56)))); // x labels that fit
  const last = bars.length - 1;
  return (
    <div ref={ref} className="chart" style={{ height, position: "relative" }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={ariaLabel} onMouseLeave={() => setHover(null)}>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={padL} x2={width - padR} y1={y(t)} y2={y(t)} className="chart-grid" />
              <text x={padL - 6} y={y(t) + 4} textAnchor="end" className="chart-axis">{compact(t)}</text>
            </g>
          ))}
          {bars.map((b, i) => {
            const cx = padL + slot * i + slot / 2;
            const h = Math.max(0, padT + plotH - y(b.value));
            const r = Math.min(4, h / 2, barW / 2);
            const x0 = cx - barW / 2, top = padT + plotH - h, base = padT + plotH;
            const d = h <= 0 ? "" : `M${x0},${base} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + barW - r} Q${x0 + barW},${top} ${x0 + barW},${top + r} V${base} Z`;
            return (
              <g key={i} onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} tabIndex={0}>
                <rect x={padL + slot * i} y={padT} width={slot} height={plotH} fill="transparent" />
                {d && <path d={d} className="chart-bar" style={{ opacity: hover === null || hover === i ? 1 : 0.45 }} />}
                {i % every === 0 && <text x={cx} y={height - 8} textAnchor="middle" className="chart-axis">{b.label}</text>}
                {i === last && b.value > 0 && hover === null && <text x={cx} y={top - 6} textAnchor="middle" className="chart-value">{format(b.value)}</text>}
              </g>
            );
          })}
        </svg>
      )}
      {hover !== null && bars[hover] && width > 0 && (
        <div className="chart-tip" style={{ left: Math.min(Math.max(padL + slot * hover + slot / 2, 90), width - 90), top: 0 }}>
          {bars[hover].tip}
        </div>
      )}
    </div>
  );
}

/** Tiny 2px line (no axes) for a row in a table; the last value gets an 8px end dot. Purely decorative: the numbers are in the row. */
export function Sparkline({ values, width = 96, height = 24 }: { values: number[]; width?: number; height?: number }) {
  if (!values.length) return null;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? (width - 8) / (values.length - 1) : 0;
  const pts = values.map((v, i) => [4 + i * step, height - 4 - (v / max) * (height - 8)]);
  return (
    <svg width={width} height={height} aria-hidden="true" className="spark">
      <polyline points={pts.map((p) => p.join(",")).join(" ")} className="spark-line" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={3} className="spark-dot" />
    </svg>
  );
}
