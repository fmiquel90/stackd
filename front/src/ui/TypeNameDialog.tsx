import { useId, useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { Button, type ButtonVariant } from "./Button";
import { Dialog, DialogContent, DialogTrigger } from "./overlays";
import { Banner, Input } from "./primitives";

// Type-the-name confirmation (DESIGN §5.7) for deletions, force-unlock and turning a safety switch
// off. A misclick safeguard: the API still enforces the permission.

export interface TypeNameDialogProps {
  /** The control that opens the dialog (rendered as the trigger). */
  trigger: ReactNode;
  title: string;
  /** What will happen, in plain words. */
  consequence: ReactNode;
  /** The name to type exactly. */
  name: string;
  confirmLabel: string;
  variant?: ButtonVariant;
  onConfirm: () => Promise<unknown>;
  /** Controlled mode (when the dialog opens from code, e.g. on Save of a settings form). */
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
}

export function TypeNameDialog({
  trigger,
  title,
  consequence,
  name,
  confirmLabel,
  variant = "danger",
  onConfirm,
  open: controlled,
  onOpenChange,
}: TypeNameDialogProps) {
  const [own, setOwn] = useState(false);
  const open = controlled ?? own;
  const setOpen = (v: boolean) => {
    (onOpenChange ?? setOwn)(v);
    if (!v) {
      setTyped("");
      setError(null);
    }
  };
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {trigger && <DialogTrigger asChild>{trigger}</DialogTrigger>}
      <DialogContent title={title}>
        <form
          className="flex flex-col gap-3"
          onSubmit={async (e) => {
            e.preventDefault();
            if (typed !== name) return;
            setBusy(true);
            setError(null);
            try {
              await onConfirm();
              setOpen(false);
            } catch (err) {
              setError(err instanceof ApiError ? (err.detail ?? err.title) : String(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="text-primary">{consequence}</div>
          <label htmlFor={id} className="flex flex-col gap-1">
            <span className="text-[12px]">
              Type <span className="font-data font-semibold">{name}</span> to confirm
            </span>
            <Input
              id={id}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              autoFocus
            />
          </label>
          {error && (
            <Banner tone="failed" role="alert" title="The API refused">
              {error}
            </Banner>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant={variant} disabled={typed !== name} loading={busy}>
              {confirmLabel}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
