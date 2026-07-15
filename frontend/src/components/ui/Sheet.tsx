import { Dialog } from "@base-ui/react/dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton } from "./IconButton";
import { cn } from "./utils";

interface SheetProps {
  open: boolean;
  title: string;
  description?: string;
  children: ReactNode;
  onOpenChange: (open: boolean) => void;
  className?: string;
}

export function Sheet({ open, title, description, children, onOpenChange, className }: SheetProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[1px] transition-opacity duration-200 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0" />
        <Dialog.Viewport className="fixed inset-0 z-50 flex justify-end">
          <Dialog.Popup
            className={cn(
              "flex h-dvh w-[min(360px,calc(100vw-24px))] flex-col border-l border-solid border-[var(--np-line)] bg-[var(--np-surface-raised)] text-[var(--np-text)] shadow-[-18px_0_48px_rgba(0,0,0,0.16)] transition-transform duration-200 data-[ending-style]:translate-x-full data-[starting-style]:translate-x-full",
              className
            )}
          >
            <header className="flex min-h-16 items-start justify-between gap-4 border-b border-solid border-[var(--np-line)] px-5 py-4">
              <div className="min-w-0">
                <Dialog.Title className="m-0 text-[15px] font-bold">{title}</Dialog.Title>
                {description && <Dialog.Description className="mt-1 text-[12px] leading-5 text-[var(--np-text-muted)]">{description}</Dialog.Description>}
              </div>
              <Dialog.Close render={<IconButton label="关闭详情" variant="ghost" tooltip={false}><X size={18} /></IconButton>} />
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
