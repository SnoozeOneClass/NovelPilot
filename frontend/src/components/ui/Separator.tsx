import { Separator as BaseSeparator } from "@base-ui/react/separator";
import type { ComponentProps } from "react";
import { cn } from "./utils";

export function Separator({ className, orientation = "horizontal", ...props }: ComponentProps<typeof BaseSeparator>) {
  return (
    <BaseSeparator
      orientation={orientation}
      className={cn(
        orientation === "horizontal" ? "h-px w-full bg-[var(--np-line)]" : "h-full w-px bg-[var(--np-line)]",
        className
      )}
      {...props}
    />
  );
}
