import { Tabs as BaseTabs } from "@base-ui/react/tabs";
import type { ComponentProps } from "react";
import { cn } from "./utils";

export const Tabs = BaseTabs.Root;

export function TabsList({ className, ...props }: ComponentProps<typeof BaseTabs.List>) {
  return (
    <BaseTabs.List
      className={cn("inline-flex min-h-9 items-center gap-1 rounded-[var(--np-radius-md)] bg-[var(--np-surface-muted)] p-1", className)}
      {...props}
    />
  );
}

export function TabsTrigger({ className, ...props }: ComponentProps<typeof BaseTabs.Tab>) {
  return (
    <BaseTabs.Tab
      className={cn("min-h-7 rounded-[var(--np-radius-sm)] px-3 text-[13px] font-semibold text-[var(--np-text-secondary)] transition-colors data-[active]:bg-[var(--np-surface-raised)] data-[active]:text-[var(--np-text)] data-[active]:shadow-sm", className)}
      {...props}
    />
  );
}

export function TabsContent({ className, ...props }: ComponentProps<typeof BaseTabs.Panel>) {
  return <BaseTabs.Panel className={cn("min-h-0 outline-none", className)} {...props} />;
}
