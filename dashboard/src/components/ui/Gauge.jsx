/** Brier score arc gauge 0–0.25 */
export function Gauge({ value, size = 80, label = "Brier" }) {
  const v = value == null ? null : Math.min(0.25, Math.max(0, value));
  const pct = v == null ? 0 : v / 0.25;
  const angle = pct * 270;
  const color = v == null ? "#52525b" : v < 0.1 ? "#22c55e" : v < 0.2 ? "#f59e0b" : "#ef4444";
  const r = size / 2 - 6;
  const cx = size / 2;
  const cy = size / 2;
  const rad = (a) => ((a - 90) * Math.PI) / 180;
  const x = cx + r * Math.cos(rad(angle));
  const y = cy + r * Math.sin(rad(angle));
  const large = angle > 180 ? 1 : 0;

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="#262626" strokeWidth="6" strokeDasharray={`${(270 / 360) * 2 * Math.PI * r} ${2 * Math.PI * r}`} transform={`rotate(135 ${cx} ${cy})`} />
      {v != null && (
        <path
          d={`M ${cx} ${cy - r} A ${r} ${r} 0 ${large} 1 ${x} ${y}`}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          transform={`rotate(135 ${cx} ${cy})`}
        />
      )}
      <text x={cx} y={cy - 2} textAnchor="middle" className="fill-text font-mono" fontSize="11">
        {v == null ? "—" : v.toFixed(3)}
      </text>
      <text x={cx} y={cy + 12} textAnchor="middle" className="fill-text-3" fontSize="8">
        {label}
      </text>
    </svg>
  );
}
