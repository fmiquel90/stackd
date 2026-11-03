import type { ComponentPropsWithoutRef, ReactNode } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as MenuPrimitive from "@radix-ui/react-dropdown-menu";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { X } from "lucide-react";
import { cn } from "./cn";

// Radix primitives re-skinned onto the tokens (DESIGN §3.4, §8): bg-raised + 1 px control border, no
// shadow; Radix provides focus trap, Escape, focus return and aria.

const SURFACE = "bg-raised text-primary border border-control rounded-base";

/* ---------- Dialog (modal) ---------- */

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  title,
  description,
  children,
  className,
  wide,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  wide?: boolean;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-scrim" />
      <DialogPrimitive.Content
        className={cn(
          SURFACE,
          "fixed top-[12vh] left-1/2 z-50 flex max-h-[76vh] w-[calc(100vw-32px)] -translate-x-1/2 flex-col overflow-auto p-5",
          wide ? "max-w-[720px]" : "max-w-[480px]",
          className,
        )}
      >
        <div className="mb-3 flex items-start justify-between gap-4">
          <DialogPrimitive.Title className="text-section font-semibold">{title}</DialogPrimitive.Title>
          <DialogPrimitive.Close aria-label="Close" className="rounded-base p-1 text-muted hover:bg-surface">
            <X size={16} strokeWidth={1.5} aria-hidden />
          </DialogPrimitive.Close>
        </div>
        {description ? (
          <DialogPrimitive.Description className="mb-3 text-muted">{description}</DialogPrimitive.Description>
        ) : (
          <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
        )}
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/* ---------- Sheet (side or bottom panel; non-modal option for the graph drawer) ---------- */

export function SheetContent({
  title,
  side = "right",
  children,
  className,
}: {
  title: string;
  side?: "right" | "bottom";
  children: ReactNode;
  className?: string;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-scrim" />
      <DialogPrimitive.Content
        className={cn(
          "fixed z-50 flex flex-col overflow-auto border-control bg-raised p-4 text-primary",
          side === "right"
            ? "top-0 right-0 h-full w-[min(480px,100vw)] border-l"
            : "right-0 bottom-0 left-0 max-h-[85vh] rounded-t-base border-t",
          className,
        )}
      >
        <div className="mb-3 flex items-center justify-between gap-4">
          <DialogPrimitive.Title className="text-section font-semibold">{title}</DialogPrimitive.Title>
          <DialogPrimitive.Close aria-label="Close" className="rounded-base p-1 text-muted hover:bg-surface">
            <X size={16} strokeWidth={1.5} aria-hidden />
          </DialogPrimitive.Close>
        </div>
        <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/* ---------- Popover ---------- */

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverAnchor = PopoverPrimitive.Anchor;
export const PopoverClose = PopoverPrimitive.Close;

export function PopoverContent({
  className,
  children,
  ...rest
}: ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        sideOffset={6}
        collisionPadding={16}
        className={cn(SURFACE, "z-50 max-w-[calc(100vw-32px)] p-3", className)}
        {...rest}
      >
        {children}
      </PopoverPrimitive.Content>
    </PopoverPrimitive.Portal>
  );
}

/* ---------- Dropdown menu ---------- */

export const Menu = MenuPrimitive.Root;
export const MenuTrigger = MenuPrimitive.Trigger;

export function MenuContent({
  className,
  children,
  ...rest
}: ComponentPropsWithoutRef<typeof MenuPrimitive.Content>) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content
        sideOffset={4}
        collisionPadding={16}
        className={cn(SURFACE, "z-50 min-w-[200px] p-1", className)}
        {...rest}
      >
        {children}
      </MenuPrimitive.Content>
    </MenuPrimitive.Portal>
  );
}

export function MenuItem({ className, ...rest }: ComponentPropsWithoutRef<typeof MenuPrimitive.Item>) {
  return (
    <MenuPrimitive.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-base px-2 py-1.5 text-ui outline-none select-none",
        "data-[highlighted]:bg-surface data-[disabled]:cursor-not-allowed data-[disabled]:text-muted",
        className,
      )}
      {...rest}
    />
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <MenuPrimitive.Label className="eyebrow px-2 pt-2 pb-1 text-muted">{children}</MenuPrimitive.Label>;
}

export function MenuSeparator() {
  return <MenuPrimitive.Separator className="my-1 h-px bg-line" />;
}

/* ---------- Tooltip ---------- */

export const TooltipProvider = TooltipPrimitive.Provider;

/** Supplementary only: never the sole place a reason or a meaning lives (DESIGN §5.2, §7). */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root delayDuration={300}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={4}
          collisionPadding={16}
          className={cn(SURFACE, "z-50 max-w-[320px] px-2 py-1 text-[12px]")}
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
