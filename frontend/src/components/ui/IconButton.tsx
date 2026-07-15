import { forwardRef, type ReactNode } from "react";
import { Button, type ButtonProps } from "./Button";
import { Tooltip } from "./Tooltip";
import { cn } from "./utils";

interface IconButtonProps extends Omit<ButtonProps, "children"> {
  label: string;
  children: ReactNode;
  tooltip?: boolean;
}

export const IconButton = forwardRef<HTMLElement, IconButtonProps>(function IconButton(
  { label, children, className, tooltip = true, ...props },
  ref
) {
  const button = (
    <Button
      ref={ref}
      aria-label={label}
      title={tooltip ? undefined : label}
      className={cn("size-9 min-h-9 shrink-0 px-0", className)}
      {...props}
    >
      {children}
    </Button>
  );
  return tooltip ? <Tooltip content={label}>{button}</Tooltip> : button;
});
