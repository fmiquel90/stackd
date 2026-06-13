import { useQuery } from "@tanstack/react-query";
import { auth } from "@/api/client";
import { useDevLogin } from "@/auth/session";

export function LoginPage() {
  const personas = useQuery({ queryKey: ["dev-personas"], queryFn: auth.devPersonas, retry: false });
  const devLogin = useDevLogin();

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div
        className="w-full max-w-[420px] rounded-base p-8"
        style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)" }}
      >
        <h1 className="text-[24px] font-semibold tracking-[-0.01em]">Stackd</h1>
        <p className="mt-1 text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
          Terraform orchestration control room.
        </p>

        <a
          href={auth.googleStartUrl}
          className="mt-6 flex items-center justify-center rounded-base px-4 py-2.5 text-[13px] font-medium"
          style={{ border: "1px solid var(--color-border)", color: "var(--color-text-primary)" }}
        >
          Sign in with Google
        </a>

        {personas.data && personas.data.personas.length > 0 && (
          <div className="mt-6">
            <div
              className="font-data mb-2 text-[12px] uppercase tracking-wide"
              style={{ color: "var(--color-text-secondary)" }}
            >
              Dev login
            </div>
            <div className="flex flex-col gap-2">
              {personas.data.personas.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  disabled={devLogin.isPending}
                  onClick={() => devLogin.mutate(p.key)}
                  className="font-data flex items-center justify-between rounded-base px-3 py-2 text-[12px] disabled:opacity-60"
                  style={{ backgroundColor: "var(--color-bg-raised)", border: "1px solid var(--color-border)" }}
                >
                  <span>{p.email}</span>
                  <span style={{ color: "var(--color-text-secondary)" }}>{p.role}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {devLogin.isError && (
          <p className="mt-3 text-[12px]" style={{ color: "var(--color-state-failed)" }}>
            Login failed. Check the API is running.
          </p>
        )}
      </div>
    </div>
  );
}
