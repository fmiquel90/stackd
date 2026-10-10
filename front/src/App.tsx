import { AppShell } from "@/app/AppShell";
import { LoginPage } from "@/app/LoginPage";
import { Walkthrough } from "@/app/Walkthrough";
import { useCompleteOnboarding, useSession } from "@/auth/session";

function Splash() {
  return (
    <div className="flex min-h-full items-center justify-center bg-base" role="status">
      <span className="font-data text-[12px] text-muted">Loading…</span>
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
