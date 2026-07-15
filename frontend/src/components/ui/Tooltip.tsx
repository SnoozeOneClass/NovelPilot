import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import type { ReactElement, ReactNode } from "react";

interface TooltipProps {
  content: ReactNode;
  children: ReactElement;
}

export function Tooltip({ content, children }: TooltipProps) {
  return (
    <BaseTooltip.Provider delay={500}>
      <BaseTooltip.Root>
        <BaseTooltip.Trigger render={children} />
        <BaseTooltip.Portal>
          <BaseTooltip.Positioner sideOffset={8} className="z-[70]">
            <BaseTooltip.Popup className="max-w-64 rounded-[var(--np-radius-sm)] bg-[var(--np-text)] px-2 py-1.5 text-[12px] leading-4 text-[var(--np-surface-raised)] shadow-lg transition-opacity data-[ending-style]:opacity-0 data-[starting-style]:opacity-0">
              {content}
            </BaseTooltip.Popup>
          </BaseTooltip.Positioner>
        </BaseTooltip.Portal>
      </BaseTooltip.Root>
    </BaseTooltip.Provider>
  );
}
