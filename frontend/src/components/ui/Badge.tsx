import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "./utils";

const badgeVariants = cva(
  "inline-flex min-h-6 items-center gap-1 rounded-full px-2 text-[12px] font-semibold",
  {
    variants: {
      tone: {
        neutral: "bg-[var(--np-surface-muted)] text-[var(--np-text-secondary)]",
        accent: "bg-[var(--np-accent-soft)] text-[var(--np-accent)]",
        success: "bg-[var(--np-success-soft)] text-[var(--np-success)]",
        danger: "bg-[var(--np-danger-soft)] text-[var(--np-danger)]"
      }
    },
    defaultVariants: { tone: "neutral" }
  }
);

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
