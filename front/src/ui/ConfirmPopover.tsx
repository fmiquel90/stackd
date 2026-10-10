import { type ReactNode } from "react";
import { Button } from "./Button";
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from "./overlays";

// Light confirmation anchored to its trigger (DESIGN §5.7: deleting a variable, a hook, an attachment,
// removing a member). The confirm button names exactly what goes ("Remove bob from demo").

export function ConfirmPopover({
  trigger,
  question,
  confirmLabel,
  onConfirm,
}: {
  trigger: ReactNode;
  question: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent align="end" className="flex w-72 flex-col gap-2">
        <p className="text-[12px]">{question}</p>
        <div className="flex justify-end gap-2">
          <PopoverClose asChild>
            <Button size="sm" variant="ghost">
              Keep
            </Button>
          </PopoverClose>
          <PopoverClose asChild>
            <Button size="sm" variant="danger" onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </PopoverClose>
        </div>
      </PopoverContent>
    </Popover>
  );
}
