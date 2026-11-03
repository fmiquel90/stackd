import { useQuery } from "@tanstack/react-query";
import { auth } from "@/api/client";
import { useDevLogin } from "@/auth/session";
import { Banner } from "@/ui/primitives";
import { Brand } from "./Brand";

// Sign-in (DESIGN §5.10): Google SSO; in dev the personas are listed with their role so they are
// told apart at a glance (their tiers are not exposed by the dev endpoint — AUDIT, needs API work).

export function LoginPage() {
  const personas = useQuery({ queryKey: ["dev-personas"], queryFn: auth.devPersonas, retry: false });
  const devLogin = useDevLogin();

  return (
    <main className="flex min-h-full items-center justify-center bg-base p-4">
      <div className="flex w-full max-w-[420px] flex-col gap-6 rounded-base border border-line bg-surface p-8 max-md:p-5">
        <div className="flex flex-col gap-1">
          <h1>
            <Brand markSize={26} textClass="text-[24px]" />
          </h1>
          <p className="text-muted">Plan, a human confirms, apply — on your own workers.</p>
        </div>

        <a
          href={auth.googleStartUrl}
          className="flex h-10 items-center justify-center rounded-base border border-control font-semibold text-primary hover:bg-raised"
        >
          Sign in with Google
        </a>

        {personas.data && personas.data.personas.length > 0 && (
          <section aria-labelledby="dev-login" className="flex flex-col gap-2">
            <h2 id="dev-login" className="eyebrow text-muted">
              Dev login — local only
            </h2>
            <ul className="flex flex-col gap-2">
              {personas.data.personas.map((p) => (
                <li key={p.key}>
                  <button
                    type="button"
                    disabled={devLogin.isPending}
                    onClick={() => devLogin.mutate(p.key)}
                    className="flex h-10 w-full items-center justify-between gap-3 rounded-base border border-line bg-raised px-3 font-data text-[12px] hover:border-control disabled:cursor-wait disabled:border-dashed"
                  >
                    <span>{p.email}</span>
                    <span className="text-muted">{p.role}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        {devLogin.isError && (
          <Banner tone="failed" role="alert" title="Sign-in failed.">
            The API did not accept the login — check that it is running.
          </Banner>
        )}
      </div>
    </main>
  );
}
