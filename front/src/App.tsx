import { AppShell } from "@/app/AppShell";
import { useCompleteOnboarding, useSession } from "@/auth/session";
import { Walkthrough } from "@/components/Walkthrough";
import { LoginPage } from "@/pages/LoginPage";

function Splash() {
  return (
    <div className="flex min-h-full items-center justify-center">
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Loading…
      </span>
    </div>
  );
}

export default function App() {
  const { data: user, isLoading } = useSession();
  const onboarding = useCompleteOnboarding();
  if (isLoading) return <Splash />;
  if (!user) return <LoginPage />;
  return (
    <>
      <AppShell user={user} />
      {/* First-login walkthrough; dismissal is persisted server-side (no browser storage). */}
      {!user.onboarded && <Walkthrough onDone={() => onboarding.mutate()} />}
    </>
  );
}
