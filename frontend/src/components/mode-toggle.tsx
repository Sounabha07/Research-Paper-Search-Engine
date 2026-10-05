"use client";

export function ModeToggle({ mode, onChange }: { mode: "quick" | "deep"; onChange: (m: "quick" | "deep") => void }) {
  const opt = (m: "quick" | "deep", label: string, title: string) => (
    <button
      type="button"
      title={title}
      onClick={() => onChange(m)}
      className={`px-2.5 py-1 ${mode === m ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
    >
      {label}
    </button>
  );
  return (
    <div className="flex overflow-hidden rounded-md border border-border text-xs" role="group" aria-label="Mode">
      {opt("quick", "Quick", "Fetch full text until 60% of the selected papers have it (up to 3 min)")}
      {opt("deep", "Deep", "Fetch full text until 80% of the selected papers have it and follow their references (up to 6 min)")}
    </div>
  );
}
