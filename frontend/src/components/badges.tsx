import { badgeList } from "@/lib/api-types";

export function Badges({ badges }: { badges: Record<string, unknown> }) {
  return (
    <span className="flex flex-wrap gap-1">
      {badgeList(badges).map((b) => (
        <span key={b} className="rounded border border-border px-1.5 py-px text-[11px] text-muted-foreground">
          {b}
        </span>
      ))}
    </span>
  );
}
