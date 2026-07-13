import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import styles from "./Dialog.module.css";

interface DialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
}

export function Dialog({ open, title, children, onClose }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      aria-labelledby="np-dialog-title"
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => { if (event.target === ref.current) onClose(); }}
    >
      <header>
        <h2 id="np-dialog-title">{title}</h2>
        <button type="button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button>
      </header>
      <div className={styles.content}>{children}</div>
    </dialog>
  );
}
