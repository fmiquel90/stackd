// StackD brand. Flat, token-driven take on the logo's layered-stack mark (the gradient/3D stays on
// the logo asset; the in-app mark is flat per DESIGN §9). The wordmark splits "Stack" (text) and the
// brand-violet "D" exactly like the logo.

export function BrandMark({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 2.5 21 7l-9 4.5L3 7l9-4.5Z" fill="var(--color-accent)" />
      <path d="M3 11l9 4.5L21 11" stroke="var(--color-accent)" strokeWidth="2" strokeLinejoin="round" opacity="0.7" />
      <path d="M3 15l9 4.5L21 15" stroke="var(--color-accent)" strokeWidth="2" strokeLinejoin="round" opacity="0.45" />
    </svg>
  );
}

export function Brand({ markSize = 18, textClass = "text-[15px]" }: { markSize?: number; textClass?: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <BrandMark size={markSize} />
      <span className={`${textClass} font-semibold tracking-[-0.01em]`}>
        Stack<span style={{ color: "var(--color-accent)" }}>D</span>
      </span>
    </span>
  );
}
