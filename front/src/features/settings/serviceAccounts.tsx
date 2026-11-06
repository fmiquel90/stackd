import { useState } from "react";
import { Bot, Plus } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { serviceAccounts, users } from "@/api/resources";
import type { ApiToken, ApiTokenCreated, ServiceAccount } from "@/api/types";
import { Button } from "@/ui/Button";
import { ConfirmPopover } from "@/ui/ConfirmPopover";
import { OneTimeSecret } from "@/ui/OneTimeSecret";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import {
  Badge,
  Banner,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  RelativeTime,
  Skeleton,
  Table,
  Td,
  Th,
} from "@/ui/primitives";
import { ProblemBanner } from "@/ui/ProblemBanner";
import {
  SERVICE_ACCOUNT_DOMAIN,
  SERVICE_ACCOUNT_NAME,
  TOKEN_DAYS,
  daysLeft,
  parseDays,
  tokenStatus,
} from "./serviceAccountModel";

// Service accounts & API tokens (SPECS §23, DESIGN §5.10) — instance admin. A token is shown once,
// held in the dialog's state only and dropped when it closes (its mutation is not kept in the
// cache either: gcTime 0 + reset). Disabling an account is the users PATCH: every token stops.

const KEY = ["service-accounts"] as const;
const tokensKey = (id: string) => ["service-accounts", id, "tokens"] as const;

/** What a service account can and cannot do — said wherever one is created or shown. */
export const SERVICE_ACCOUNT_LIMITS =
  "A service account can read and trigger plans, and do what its space roles allow outside the apply gate (configuration, export / import). It never confirms an apply, approves a rollout or destroys: every apply stays a named human's decision.";

function NewServiceAccountDialog() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<"reader" | "writer">("reader");
  const valid = SERVICE_ACCOUNT_NAME.test(name);
  const create = useMutation({
    mutationFn: () => serviceAccounts.create({ name, role }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: KEY });
      qc.invalidateQueries({ queryKey: ["users"] });
      setOpen(false);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) {
          setName("");
          setRole("reader");
          create.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="secondary" icon={<Plus size={14} strokeWidth={1.75} aria-hidden />}>
          New service account
        </Button>
      </DialogTrigger>
      <DialogContent title="New service account" description={SERVICE_ACCOUNT_LIMITS}>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) create.mutate();
          }}
        >
          <Field
            label="Name"
            help={
              name && !valid ? (
                "Lowercase letters, digits and dashes; starts with a letter or digit; 63 characters at most."
              ) : (
                <>
                  Signs as{" "}
                  <span className="font-data">
                    {name || "<name>"}@{SERVICE_ACCOUNT_DOMAIN}
                  </span>{" "}
                  in the audit.
                </>
              )
            }
          >
            <Input
              value={name}
              onChange={(e) => setName(e.target.value.trim())}
              placeholder="ci-export"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={!!name && !valid}
              required
            />
          </Field>
          <Field
            label="Instance role"
            help="Reader or writer only. It reaches no space until you add it as a member under Spaces."
          >
            <NativeSelect value={role} onChange={(e) => setRole(e.target.value as "reader" | "writer")}>
              <option value="reader">reader</option>
              <option value="writer">writer</option>
            </NativeSelect>
          </Field>
          {create.isError && <ProblemBanner error={create.error} />}
          <div className="flex justify-end gap-2">
            <Button type="submit" variant="primary" disabled={!valid} loading={create.isPending}>
              Create {name || "service account"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TokenStatusBadge({ token }: { token: ApiToken }) {
  const status = tokenStatus(token);
  if (status === "revoked") return <Badge glyph="⊘">revoked</Badge>;
  if (status === "expired") return <Badge glyph="◷">expired</Badge>;
  const left = daysLeft(token.expires_at);
  return (
    <Badge glyph="●" title={`Expires in ${left} day${left === 1 ? "" : "s"}`}>
      active
    </Badge>
  );
}

function TokenRow({ account, token }: { account: ServiceAccount; token: ApiToken }) {
  const qc = useQueryClient();
  const revoke = useMutation({
    mutationFn: () => serviceAccounts.revokeToken(account.id, token.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: tokensKey(account.id) });
      qc.invalidateQueries({ queryKey: KEY });
    },
  });
  const status = tokenStatus(token);
  return (
    <tr>
      <Td className="font-data">{token.name}</Td>
      <Td className="font-data text-[12px]">{token.prefix}…</Td>
      <Td>
        <TokenStatusBadge token={token} />
      </Td>
      <Td className="text-[12px]">
        <RelativeTime value={token.revoked_at ?? token.expires_at} />
      </Td>
      <Td className="text-[12px]">
        <RelativeTime value={token.last_used_at} />
      </Td>
      <Td className="text-right">
        {status === "active" && (
          <ConfirmPopover
            trigger={
              <Button size="sm" variant="ghost">
                Revoke…
              </Button>
            }
            question={`Anything still using ${token.prefix}… gets a 401 from now on. This cannot be undone.`}
            confirmLabel={`Revoke token ${token.name}`}
            onConfirm={() => revoke.mutate()}
          />
        )}
        {revoke.isError && <ProblemBanner error={revoke.error} />}
      </Td>
    </tr>
  );
}

/** The account's tokens, and "New token": the raw value lives here, until the dialog closes. */
export function TokensDialog({ account }: { account: ServiceAccount }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [days, setDays] = useState(String(TOKEN_DAYS.default));
  const [created, setCreated] = useState<ApiTokenCreated | null>(null);
  const tokens = useQuery({
    queryKey: tokensKey(account.id),
    queryFn: () => serviceAccounts.tokens(account.id),
    enabled: open,
  });
  const expiresIn = parseDays(days);
  const create = useMutation({
    mutationFn: () => serviceAccounts.createToken(account.id, { name, expires_in_days: expiresIn! }),
    // The response carries the raw token: drop the mutation from the cache as soon as it is reset.
    gcTime: 0,
    onSuccess: (t) => {
      setCreated(t);
      setName("");
      qc.invalidateQueries({ queryKey: tokensKey(account.id) });
      qc.invalidateQueries({ queryKey: KEY });
    },
  });
  const close = () => {
    setCreated(null);
    create.reset();
    setName("");
    setDays(String(TOKEN_DAYS.default));
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) close();
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost">
          Tokens…
        </Button>
      </DialogTrigger>
      <DialogContent
        wide
        title={`API tokens of ${account.name}`}
        description={
          <>
            Sent as <span className="font-data">Authorization: Bearer stkd_…</span> on the API. A token always
            expires; revoke one as soon as it is not needed.
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {created && <OneTimeSecret label={`Token ${created.name}`} value={created.token} />}
          {tokens.isLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : tokens.isError ? (
            <ProblemBanner error={tokens.error} />
          ) : (tokens.data ?? []).length === 0 ? (
            <p className="text-muted">No token yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Prefix</Th>
                    <Th>Status</Th>
                    <Th>Expires / revoked</Th>
                    <Th>Last used</Th>
                    <Th>
                      <span className="sr-only">Actions</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {(tokens.data ?? []).map((t) => (
                    <TokenRow key={t.id} account={account} token={t} />
                  ))}
                </tbody>
              </Table>
            </div>
          )}
          {account.disabled ? (
            <Banner tone="info" title="This account is disabled">
              Enable it to create a token: while disabled, every token gets a 401.
            </Banner>
          ) : (
            <form
              className="flex flex-wrap items-end gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (name && expiresIn) create.mutate();
              }}
            >
              <Field label="New token">
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="github-actions"
                  maxLength={100}
                  spellCheck={false}
                  autoComplete="off"
                  required
                />
              </Field>
              <Field label="Expires in (days)">
                <Input
                  value={days}
                  onChange={(e) => setDays(e.target.value)}
                  inputMode="numeric"
                  className="w-24"
                  aria-invalid={!expiresIn}
                  aria-describedby={`${account.id}-days-help`}
                />
              </Field>
              <Button
                type="submit"
                variant="primary"
                disabled={!name || !expiresIn}
                loading={create.isPending}
              >
                Create token
              </Button>
              <span id={`${account.id}-days-help`} className="basis-full text-[12px] text-muted">
                {TOKEN_DAYS.min} to {TOKEN_DAYS.max} days.
              </span>
            </form>
          )}
          {create.isError && <ProblemBanner error={create.error} />}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function AccountRow({ account }: { account: ServiceAccount }) {
  const qc = useQueryClient();
  const setDisabled = useMutation({
    mutationFn: (disabled: boolean) => users.update(account.id, { disabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: KEY });
      qc.invalidateQueries({ queryKey: ["users"] });
    },
  });
  return (
    <li className="flex flex-col gap-2 rounded-base border border-line bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Bot size={14} strokeWidth={1.5} className="text-muted" aria-hidden />
        <span className="font-data font-semibold">{account.name}</span>
        <Badge>{account.role}</Badge>
        {account.disabled && <Badge glyph="⊘">disabled</Badge>}
        <span className="font-data text-[12px] text-muted">
          · {account.active_tokens} active token{account.active_tokens === 1 ? "" : "s"} · created{" "}
          <RelativeTime value={account.created_at} />
        </span>
        <span className="ml-auto flex flex-wrap gap-2">
          <TokensDialog account={account} />
          {account.disabled ? (
            <Button
              size="sm"
              variant="ghost"
              loading={setDisabled.isPending}
              onClick={() => setDisabled.mutate(false)}
            >
              Enable
            </Button>
          ) : (
            <ConfirmPopover
              trigger={
                <Button size="sm" variant="ghost">
                  Disable…
                </Button>
              }
              question={`Every token of ${account.name} stops working at once (401). Its tokens are kept: enabling it again revives the unexpired ones.`}
              confirmLabel={`Disable ${account.name}`}
              onConfirm={() => setDisabled.mutate(true)}
            />
          )}
        </span>
      </div>
      <span className="font-data text-[12px] text-muted">{account.email}</span>
      {setDisabled.isError && <ProblemBanner error={setDisabled.error} />}
    </li>
  );
}

export function ServiceAccounts() {
  const { data, isLoading, isError, error } = useQuery({ queryKey: KEY, queryFn: serviceAccounts.list });
  return (
    <section aria-labelledby="service-accounts" className="flex flex-col gap-2">
      <h2 id="service-accounts" className="text-section font-semibold">
        Service accounts
      </h2>
      <p className="max-w-[720px] text-[12px] text-muted">
        Identities for automation (a CI job, a scheduled export), authenticated with expiring API tokens.{" "}
        {SERVICE_ACCOUNT_LIMITS} Grant one a space under Spaces, below.
      </p>
      {isLoading ? (
        <Skeleton className="h-16 w-full max-w-[960px]" />
      ) : isError ? (
        <ProblemBanner error={error} />
      ) : (data ?? []).length === 0 ? (
        <div className="max-w-[960px]">
          <EmptyState title="No service account yet">
            Create one for each automation, so its actions are audited under its own name.
          </EmptyState>
        </div>
      ) : (
        <ul className="flex max-w-[960px] flex-col gap-2">
          {(data ?? []).map((a) => (
            <AccountRow key={a.id} account={a} />
          ))}
        </ul>
      )}
      <div>
        <NewServiceAccountDialog />
      </div>
    </section>
  );
}
