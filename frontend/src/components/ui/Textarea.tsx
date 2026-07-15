import { forwardRef, type TextareaHTMLAttributes } from "react";
import { cn } from "./utils";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...props },
  ref
) {
  return (
    <textarea
      ref={ref}
      className={cn("min-h-28 w-full resize-y rounded-[var(--np-radius-md)] border border-solid border-[var(--np-line)] bg-[var(--np-surface-raised)] px-3 py-2.5 text-[15px] leading-[1.65] text-[var(--np-text)] placeholder:text-[var(--np-text-muted)] focus:border-[var(--np-accent)] focus:outline-none disabled:bg-[var(--np-surface-muted)]", className)}
      {...props}
    />
  );
});
