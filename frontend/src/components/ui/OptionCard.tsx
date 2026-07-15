import { Button as BaseButton, type ButtonProps as BaseButtonProps } from "@base-ui/react/button";
import { Check } from "lucide-react";
import { forwardRef, type ReactNode } from "react";
import { Badge } from "./Badge";
import { cn } from "./utils";

interface OptionCardProps extends BaseButtonProps {
  marker?: ReactNode;
  title: string;
  description?: string | null;
  detail?: string | null;
  selected?: boolean;
  recommended?: boolean;
}

export const OptionCard = forwardRef<HTMLElement, OptionCardProps>(function OptionCard(
  { marker, title, description, detail, selected = false, recommended = false, className, ...props },
  ref
) {
  return (
    <BaseButton
      ref={ref}
      className={cn(
        "group flex w-full items-start gap-3 rounded-[var(--np-radius-lg)] border border-solid border-[var(--np-line)] bg-[var(--np-surface-raised)] p-3 text-left transition-colors hover:border-[var(--np-line-strong)] hover:bg-[var(--np-surface)]",
        selected && "border-[var(--np-accent)] bg-[var(--np-accent-soft)]",
        className
      )}
      {...props}
    >
      <span className="grid size-7 shrink-0 place-items-center rounded-full bg-[var(--np-surface-muted)] text-[12px] font-bold text-[var(--np-text-secondary)] group-data-[pressed]:bg-[var(--np-accent)] group-data-[pressed]:text-white">
        {selected ? <Check size={15} /> : marker}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2 text-[13px] font-bold text-[var(--np-text)]">
          {title}
          {recommended && <Badge tone="accent">推荐</Badge>}
        </span>
        {description && <span className="mt-1 block text-[13px] leading-5 text-[var(--np-text-secondary)]">{description}</span>}
        {detail && <span className="mt-2 block text-[12px] leading-5 text-[var(--np-text-muted)]">{detail}</span>}
      </span>
    </BaseButton>
  );
});
