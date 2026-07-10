export function Skeleton({ className = "h-4 w-full" }) {
  return <div className={`animate-pulse bg-surface-2 rounded ${className}`} />;
}
