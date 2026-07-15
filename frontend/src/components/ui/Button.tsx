import { Button as BaseButton, type ButtonProps as BaseButtonProps } from "@base-ui/react/button";
import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef } from "react";
import { cn } from "./utils";

export const buttonVariants = cva(
  "inline-flex min-h-9 items-center justify-center gap-2 rounded-[var(--np-radius-md)] px-3 text-[13px] font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--np-focus)] disabled:pointer-events-none disabled:opacity-55",
  {
    variants: {
      variant: {
        primary: "bg-[var(--np-accent)] text-white hover:bg-[var(--np-accent-hover)]",
        secondary: "border border-solid border-[var(--np-line)] bg-[var(--np-surface-raised)] text-[var(--np-text)] hover:bg-[var(--np-surface-muted)]",
        ghost: "bg-transparent text-[var(--np-text-secondary)] hover:bg-[var(--np-surface-muted)] hover:text-[var(--np-text)]",
        danger: "bg-[var(--np-danger)] text-white hover:brightness-95"
      },
      size: {
        sm: "min-h-8 px-2.5",
        md: "min-h-9 px-3",
        lg: "min-h-11 px-4 text-[14px]"
      }
    },
    defaultVariants: { variant: "secondary", size: "md" }
  }
);

export interface ButtonProps extends BaseButtonProps, VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLElement, ButtonProps>(function Button(
  { className, variant, size, type = "button", ...props },
  ref
) {
  return <BaseButton ref={ref} type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});
