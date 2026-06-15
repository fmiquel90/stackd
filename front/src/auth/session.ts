import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, auth } from "@/api/client";
import type { User } from "@/api/types";

const SESSION_KEY = ["session"] as const;

/** Boot the session by exchanging the refresh cookie for an access token (DESIGN §6). */
export function useSession() {
  return useQuery<User | null>({
    queryKey: SESSION_KEY,
    queryFn: async () => {
      try {
        const session = await auth.refresh();
        return session.user;
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          return null;
        }
        throw err;
      }
    },
    retry: false,
    staleTime: 10 * 60 * 1000,
  });
}

/** Current user's admin status, read from the cached session (no extra request). */
export function useIsAdmin(): boolean {
  return useSession().data?.role === "admin";
}

export function useDevLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (persona: string) => auth.devLogin(persona),
    onSuccess: (session) => qc.setQueryData(SESSION_KEY, session.user),
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => auth.logout(),
    onSuccess: () => qc.setQueryData(SESSION_KEY, null),
  });
}

export function useCompleteOnboarding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => auth.markOnboarded(),
    onSuccess: (user) => qc.setQueryData(SESSION_KEY, user),
  });
}
