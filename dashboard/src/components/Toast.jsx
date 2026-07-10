import { useAtlasStore } from "../store/AtlasContext";

const variants = {
  success: "border-green/40 bg-green/10 text-green",
  error: "border-red/40 bg-red/10 text-red",
  warn: "border-amber/40 bg-amber/10 text-amber",
};

export default function Toast() {
  const { state, dispatch } = useAtlasStore();
  const toasts = state.controls?.toasts || [];

  if (!toasts.length) return null;

  return (
    <div className="fixed top-14 right-4 z-[100] flex flex-col gap-2 max-w-sm pointer-events-none">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`pointer-events-auto px-4 py-2 rounded border text-sm font-mono shadow-lg animate-[slideIn_0.2s_ease-out] ${variants[t.type] || variants.warn}`}
          onClick={() => dispatch({ type: "DISMISS_TOAST", id: t.id })}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
