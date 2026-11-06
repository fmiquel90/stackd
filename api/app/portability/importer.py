"""Import a `ConfigDocument` into a space (SPECS §22.3).

Merge semantics: every object of the document is created when missing and updated when it differs,
matched by its natural key; nothing absent from the document is ever deleted. All or nothing, in
the request's transaction; a dry run computes the same report and rolls back. Every write is
audited exactly like its endpoint would audit it (context `via: import`), in the same transaction.
Secrets never travel in a document: what the import cannot create without one is reported in
`missing_secrets`, never invented.
"""

from __future__ import annotations

import secrets as pysecrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.dependencies.router import _create_edge
from app.enums import AttachmentTarget, AuditActorKind, RepoAuthKind, Role, SecretFallback
from app.environments.router import lock_attach_target
from app.errors import ProblemException
from app.models.dependency import EnvDependency, OutputReference
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.notification import NotificationTarget
from app.models.oidc import CloudIntegration
from app.models.secret_source import SecretSource
from app.models.space import Space
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.models.variable import Variable
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.models.worker import WorkerPool
from app.notifications.dispatcher import check_url_safe
from app.portability.schemas import (
    AttachmentDoc,
    CloudIntegrationDoc,
    ConfigDocument,
    DependencyDoc,
    EnvironmentDoc,
    HookDoc,
    NotificationTargetDoc,
    VariableDoc,
)
from app.security import hash_token
from app.spaces import require_space_access
from app.variables.crud import variables_for
from app.variables.values import set_reference, store_value

# Environment settings carried by a document (§3.2) — never the operational columns (head, drift
# status, `locked`), which describe the source instance, not the configuration.
_ENV_FIELDS = (
    "tier",
    "branch",
    "autodeploy",
    "protected",
    "require_second_pair_of_eyes",
    "managed_state",
    "allow_mock_apply",
    "allow_fallback_apply",
    "drift_check_enabled",
    "backend_config_file",
    "backend_config",
    "labels",
    "position",
)
_STACK_FIELDS = (
    "description",
    "repo_url",
    "repo_auth_kind",
    "project_root",
    "tool",
    "tool_version",
    "labels",
)


def _assign(obj: Any, values: dict[str, Any]) -> list[str]:
    """Set the fields that differ; return their names (sorted), for the report and the audit."""
    changed = []
    for name, value in values.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            changed.append(name)
    return sorted(changed)


@dataclass
class _PendingEnv:
    env: Environment
    stack_name: str
    wanted_autodeploy: bool


@dataclass
class ImportReport:
    dry_run: bool
    changes: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    missing_secrets: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    id_map: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"stacks": {}, "environments": {}}
    )
    worker_pool_tokens: list[dict[str, str]] = field(default_factory=list)

    def change(self, op: str, kind: str, path: str, fields: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"op": op, "kind": kind, "path": path}
        if fields:
            entry["fields"] = fields
        self.changes.append(entry)

    def skip(self, kind: str, path: str, reason: str, **extra: Any) -> None:
        self.skipped.append({"kind": kind, "path": path, "reason": reason, **extra})

    def as_dict(self) -> dict[str, Any]:
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        for c in self.changes:
            counts[c["op"]] += 1
        return {
            "dry_run": self.dry_run,
            "summary": {**counts, "skipped": len(self.skipped)},
            "changes": self.changes,
            "skipped": self.skipped,
            "missing_secrets": self.missing_secrets,
            "warnings": self.warnings,
            "id_map": self.id_map,
            # Shown once, like POST /worker-pools; never in a dry run (nothing was created).
            "worker_pool_tokens": [] if self.dry_run else self.worker_pool_tokens,
        }


class Importer:
    def __init__(
        self,
        session: AsyncSession,
        user: User,
        space: Space,
        doc: ConfigDocument,
        *,
        dry_run: bool,
    ) -> None:
        self.s = session
        self.user = user
        self.space = space
        self.doc = doc
        self.instance_admin = user.role == Role.admin
        self.report = ImportReport(dry_run=dry_run)
        self.tier_ids: dict[str, uuid.UUID] = {}
        self.source_ids: dict[str, uuid.UUID] = {}
        self.stacks: dict[str, Stack] = {}
        self.envs: dict[tuple[str, str], Environment] = {}
        self.sets: dict[str, VariableSet] = {}
        self.pending_envs: list[_PendingEnv] = []

    # -- helpers ---------------------------------------------------------------------------------

    async def _audit(
        self, action: str, target_kind: str, target_id: uuid.UUID | None, /, **context: Any
    ) -> None:
        await record_audit(
            self.s,
            action=action,
            actor_kind=AuditActorKind.user,
            actor_id=self.user.id,
            actor_email=self.user.email,
            target_kind=target_kind,
            target_id=target_id,
            context={**context, "via": "import"},
        )

    def _missing(self, path: str, item: str, **extra: Any) -> None:
        self.report.missing_secrets.append({"path": path, "item": item, **extra})

    # -- entry point -----------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        await self._tiers()
        await self._secret_sources()
        await self._worker_pools()
        await self._load_space()
        for st in self.doc.stacks:
            await self._stack(st)
        for vs in self.doc.variable_sets:
            await self._variable_set(vs.name, vs)
        for vs in self.doc.variable_sets:
            for att in vs.attachments:
                await self._attachment(self.sets[vs.name], att)
        for st in self.doc.stacks:
            await self._stack_children(st)
        for dep in self.doc.dependencies:
            await self._dependency(dep)
        await self._finish_new_envs()
        report = self.report.as_dict()
        await self._audit(
            "space.config_imported",
            "space",
            self.space.id,
            summary=report["summary"],
            source=self.doc.source.model_dump(mode="json") if self.doc.source else None,
            missing_secrets=len(self.report.missing_secrets),
        )
        return report

    # -- catalog-level items ---------------------------------------------------------------------

    async def _tiers(self) -> None:
        existing = {t.name: t for t in (await self.s.execute(select(Tier))).scalars()}
        needed = {e.tier for st in self.doc.stacks for e in st.environments} | {
            a.tier for vs in self.doc.variable_sets for a in vs.attachments if a.tier
        }
        declared = {t.name for t in self.doc.tiers}
        unknown = sorted(needed - existing.keys() - declared)
        if unknown:
            raise ProblemException(
                422, "Unknown tier", f"Not in the catalog nor in the document: {', '.join(unknown)}"
            )
        to_create = [t for t in self.doc.tiers if t.name not in existing]
        if to_create and not self.instance_admin:
            # The catalog is instance-wide (§2.4): a space import never edits it on its own.
            raise ProblemException(
                403,
                "Forbidden",
                "Creating tiers requires the instance admin role: "
                + ", ".join(t.name for t in to_create),
            )
        for t in self.doc.tiers:
            row = existing.get(t.name)
            if row is None:
                row = Tier(
                    name=t.name,
                    requires_four_eyes=t.requires_four_eyes,
                    repo_hooks_at_apply=t.repo_hooks_at_apply,
                    position=t.position,
                )
                self.s.add(row)
                await self.s.flush()
                await self._audit(
                    "tier.created",
                    "tier",
                    row.id,
                    name=row.name,
                    requires_four_eyes=row.requires_four_eyes,
                    repo_hooks_at_apply=row.repo_hooks_at_apply,
                )
                self.report.change("created", "tier", f"tiers/{t.name}")
                existing[t.name] = row
                continue
            # An existing tier is never modified by a space import: its gates (four-eyes, repo
            # hooks at apply) govern every space. A difference is reported, not applied.
            differs = sorted(
                f
                for f in ("requires_four_eyes", "repo_hooks_at_apply")
                if getattr(row, f) != getattr(t, f)
            )
            if differs:
                self.report.warnings.append(
                    {"kind": "tier_differs", "path": f"tiers/{t.name}", "fields": differs}
                )
            self.report.change("unchanged", "tier", f"tiers/{t.name}")
        self.tier_ids = {name: t.id for name, t in existing.items()}

    async def _secret_sources(self) -> None:
        existing = {
            s.name: s
            for s in (
                await self.s.execute(
                    select(SecretSource).where(SecretSource.space_id == self.space.id)
                )
            ).scalars()
        }
        for doc in self.doc.secret_sources:
            path = f"secret_sources/{doc.name}"
            row = existing.get(doc.name)
            if row is None:
                # A source cannot exist without its bootstrap credential, and a document never
                # carries one: create it through the API, then re-import.
                self.report.skip("secret_source", path, "bootstrap_secret_required")
                self._missing(path, "bootstrap_secret")
                continue
            if row.provider != doc.provider:
                raise ProblemException(
                    422,
                    "Secret source provider mismatch",
                    f"'{doc.name}' is a {row.provider.value} source here, "
                    f"{doc.provider.value} in the document.",
                )
            if row.config != doc.config and not self.instance_admin:
                # Re-pointing a source (its server, its scope) is an instance admin's call, as on
                # PATCH …/secret-sources (§15): the stored credential would follow the new config.
                self.report.skip("secret_source", path, "instance_admin_required")
                self.source_ids[doc.name] = row.id  # the source itself is usable as it is
                continue
            changed = _assign(row, {"config": doc.config})
            if changed:
                await self._audit(
                    "secret_source.updated", "secret_source", row.id, name=row.name, fields=changed
                )
            self.report.change(
                "updated" if changed else "unchanged", "secret_source", path, changed
            )
            self.source_ids[doc.name] = row.id

    async def _worker_pools(self) -> None:
        existing = {p.name: p for p in (await self.s.execute(select(WorkerPool))).scalars()}
        for doc in self.doc.worker_pools:
            path = f"worker_pools/{doc.name}"
            row = existing.get(doc.name)
            if row is not None:
                if row.space_id != self.space.id:
                    # Pool names are instance-wide; another space's pool is not this one's.
                    self.report.skip("worker_pool", path, "name_taken_in_another_space")
                elif row.labels != doc.labels:
                    # A pool's labels route runs (§3.7); they change only through the pool's own
                    # admin, never as a side effect of an import.
                    self.report.warnings.append({"kind": "worker_pool_labels_differ", "path": path})
                    self.report.change("unchanged", "worker_pool", path)
                else:
                    self.report.change("unchanged", "worker_pool", path)
                continue
            if not self.instance_admin:
                self.report.skip("worker_pool", path, "instance_admin_required")
                continue
            token = pysecrets.token_urlsafe(32)
            pool = WorkerPool(
                space_id=self.space.id,
                name=doc.name,
                labels=doc.labels,
                token_hash=hash_token(token),
            )
            self.s.add(pool)
            await self.s.flush()
            await self._audit("worker_pool.created", "worker_pool", pool.id, name=pool.name)
            self.report.change("created", "worker_pool", path)
            self.report.worker_pool_tokens.append({"name": pool.name, "token": token})

    # -- stacks & environments -------------------------------------------------------------------

    async def _load_space(self) -> None:
        for stack in (
            await self.s.execute(select(Stack).where(Stack.space_id == self.space.id))
        ).scalars():
            self.stacks[stack.name] = stack
        if self.stacks:
            by_id = {s.id: s.name for s in self.stacks.values()}
            for env in (
                await self.s.execute(
                    select(Environment).where(Environment.stack_id.in_(list(by_id)))
                )
            ).scalars():
                self.envs[(by_id[env.stack_id], env.name)] = env
        for vs in (
            await self.s.execute(select(VariableSet).where(VariableSet.space_id == self.space.id))
        ).scalars():
            self.sets[vs.name] = vs

    async def _stack(self, doc: Any) -> None:
        path = f"stacks/{doc.name}"
        values = {f: getattr(doc, f) for f in _STACK_FIELDS}
        stack = self.stacks.get(doc.name)
        if stack is None:
            stack = Stack(space_id=self.space.id, name=doc.name, **values)
            self.s.add(stack)
            await self.s.flush()
            await self._audit(
                "stack.created", "stack", stack.id, name=stack.name, repo_url=stack.repo_url
            )
            self.report.change("created", "stack", path)
            self.stacks[doc.name] = stack
        else:
            changed = _assign(stack, values)
            if changed:
                await self._audit("stack.updated", "stack", stack.id, fields=changed)
            self.report.change("updated" if changed else "unchanged", "stack", path, changed)
        if doc.id is not None:
            self.report.id_map["stacks"][str(doc.id)] = str(stack.id)
        if doc.repo_auth_kind != RepoAuthKind.none and stack.repo_secret_encrypted is None:
            self._missing(path, "repo_secret")
        if doc.has_webhook_secret and stack.webhook_secret_encrypted is None:
            self._missing(path, "webhook_secret")
        for env_doc in doc.environments:
            await self._environment(stack, env_doc)

    async def _environment(self, stack: Stack, doc: EnvironmentDoc) -> None:
        path = f"stacks/{stack.name}/environments/{doc.name}"
        values = {f: getattr(doc, f) for f in _ENV_FIELDS}
        if values["protected"]:
            values["autodeploy"] = False  # protected forces manual confirmation (§3.2)
        env = self.envs.get((stack.name, doc.name))
        if env is None:
            # Created with autodeploy off; `_finish_new_envs` turns it on once the import knows no
            # secret is left to fill (a push must not apply before them, cf. §21.2).
            wanted = values.pop("autodeploy")
            env = Environment(stack_id=stack.id, name=doc.name, autodeploy=False, **values)
            self.s.add(env)
            await self.s.flush()
            self.pending_envs.append(_PendingEnv(env, stack.name, wanted))
            self.report.change("created", "environment", path)
            self.envs[(stack.name, doc.name)] = env
        else:
            if values["branch"] != env.branch:
                # Same as PATCH (§9.6): the recorded head was the old branch's.
                env.head_sha = None
                env.head_updated_at = None
                env.commits_ahead = None
                env.affects_project_root = None
            changed = _assign(env, values)
            if changed:
                await self._audit(
                    "environment.updated",
                    "environment",
                    env.id,
                    fields=changed,
                    name=env.name,
                    tier=env.tier,
                )
            self.report.change("updated" if changed else "unchanged", "environment", path, changed)
        if doc.id is not None:
            self.report.id_map["environments"][str(doc.id)] = str(env.id)

    async def _finish_new_envs(self) -> None:
        left_to_fill = bool(self.report.missing_secrets)
        for p in self.pending_envs:
            reason = None
            if p.wanted_autodeploy:
                if left_to_fill:
                    reason = "left_to_fill"
                    self.report.warnings.append(
                        {
                            "kind": "autodeploy_off",
                            "path": f"stacks/{p.stack_name}/environments/{p.env.name}",
                            "reason": reason,
                        }
                    )
                else:
                    p.env.autodeploy = True
            await self._audit(
                "environment.created",
                "environment",
                p.env.id,
                name=p.env.name,
                tier=p.env.tier,
                autodeploy=p.env.autodeploy,
                **({"autodeploy_off_reason": reason} if reason else {}),
            )

    async def _stack_children(self, doc: Any) -> None:
        stack = self.stacks[doc.name]
        path = f"stacks/{doc.name}"
        await self._variables(doc.variables, path, scope="stack", stack_id=stack.id)
        await self._hooks(AttachmentTarget.stack, stack.id, doc.hooks, path)
        await self._notifications(AttachmentTarget.stack, stack.id, doc.notification_targets, path)
        for env_doc in doc.environments:
            env = self.envs[(doc.name, env_doc.name)]
            env_path = f"{path}/environments/{env_doc.name}"
            await self._variables(
                env_doc.variables, env_path, scope="env", stack_id=stack.id, environment_id=env.id
            )
            await self._hooks(AttachmentTarget.environment, env.id, env_doc.hooks, env_path)
            await self._notifications(
                AttachmentTarget.environment, env.id, env_doc.notification_targets, env_path
            )
            if env_doc.cloud_integration is not None:
                await self._cloud(env, env_doc.cloud_integration, env_path)

    # -- variables & sets ------------------------------------------------------------------------

    async def _variable_set(self, name: str, doc: Any) -> None:
        path = f"variable_sets/{name}"
        values = {
            "description": doc.description,
            "auto_attach": doc.auto_attach,
            "selector": doc.selector,
        }
        vs = self.sets.get(name)
        if vs is None:
            vs = VariableSet(space_id=self.space.id, name=name, **values)
            self.s.add(vs)
            await self.s.flush()
            await self._audit("variable_set.created", "variable_set", vs.id, name=vs.name)
            self.report.change("created", "variable_set", path)
            self.sets[name] = vs
        else:
            changed = _assign(vs, values)
            if changed:
                await self._audit("variable_set.updated", "variable_set", vs.id, fields=changed)
            self.report.change("updated" if changed else "unchanged", "variable_set", path, changed)
        await self._variables(doc.variables, path, scope="set", variable_set_id=vs.id)

    async def _variables(
        self,
        docs: list[VariableDoc],
        owner_path: str,
        *,
        scope: str,
        stack_id: uuid.UUID | None = None,
        environment_id: uuid.UUID | None = None,
        variable_set_id: uuid.UUID | None = None,
    ) -> None:
        existing = {
            (v.kind, v.name): v
            for v in await variables_for(
                self.s,
                stack_id=stack_id,
                environment_id=environment_id,
                variable_set_id=variable_set_id,
            )
        }
        for doc in docs:
            path = f"{owner_path}/variables/{doc.kind.value}/{doc.name}"
            var = existing.get((doc.kind, doc.name))
            created = var is None
            if var is None:
                var = Variable(
                    stack_id=stack_id,
                    environment_id=environment_id,
                    variable_set_id=variable_set_id,
                    kind=doc.kind,
                    name=doc.name,
                    hcl=doc.hcl,
                )
            changed: list[str] = []
            if doc.secret is not None:
                source_id = self.source_ids.get(doc.secret.source)
                if source_id is None:
                    self.report.skip(
                        "variable", path, "secret_source_missing", source=doc.secret.source
                    )
                    continue
                mode = doc.secret.fallback_mode
                keep_fallback = (
                    not created
                    and var.secret_source_id is not None
                    and var.secret_fallback_encrypted is not None
                    and mode == SecretFallback.static
                )
                if mode == SecretFallback.static and not keep_fallback:
                    # The static fallback is a literal secret the document cannot carry: the
                    # reference is imported with the fail-closed mode until one is supplied.
                    self._missing(path, "secret_fallback")
                    mode = SecretFallback.error
                same = (
                    not created
                    and var.secret_source_id == source_id
                    and var.secret_ref == doc.secret.ref
                    and var.secret_fallback_mode == mode
                )
                if not same:
                    fallback = var.secret_fallback_encrypted if keep_fallback else None
                    set_reference(
                        var,
                        source_id=source_id,
                        secret_ref=doc.secret.ref,
                        fallback_mode=mode,
                        fallback_value=None,
                    )
                    var.secret_fallback_encrypted = fallback
                    changed.append("secret")
            elif doc.value is not None:
                # A plain value, or a sensitive one filled in by hand: written like the API does.
                if (
                    created
                    or var.secret_source_id is not None
                    or var.sensitive != doc.sensitive
                    or doc.sensitive  # an encrypted value can't be compared: always rewritten
                    or var.value != doc.value
                ):
                    store_value(var, doc.value, sensitive=doc.sensitive)
                    changed.append("value")
            else:
                # Sensitive, no value in the document (the export never writes one).
                if created:
                    self.report.skip("variable", path, "value_required")
                    self._missing(path, "variable")
                    continue
                if var.secret_source_id is not None:
                    self.report.skip("variable", path, "value_required")
                    self._missing(path, "variable")
                    continue
                if not var.sensitive:
                    assert var.value is not None
                    store_value(var, var.value, sensitive=True)  # flag it; the value is kept
                    changed.append("sensitive")
            # `hcl` last: a variable skipped above is left untouched (no unaudited change).
            if not created and var.hcl != doc.hcl:
                var.hcl = doc.hcl
                changed.append("hcl")
            if created:
                self.s.add(var)
                await self.s.flush()
            if created or changed:
                await self._audit(
                    "variable.created" if created else "variable.updated",
                    "variable",
                    var.id,
                    name=var.name,
                    kind=var.kind.value,
                    sensitive=var.sensitive,
                    scope=scope,
                    **({} if created else {"fields": sorted(set(changed))}),
                )
            self.report.change(
                "created" if created else ("updated" if changed else "unchanged"),
                "variable",
                path,
                None if created else sorted(set(changed)),
            )

    async def _attachment(self, vs: VariableSet, doc: AttachmentDoc) -> None:
        if doc.target == "tier":
            assert doc.tier is not None
            kind, target_id, label = AttachmentTarget.tier, self.tier_ids[doc.tier], doc.tier
        elif doc.target == "stack":
            stack = self.stacks.get(doc.stack or "")
            if stack is None:
                self.report.skip(
                    "attachment", f"variable_sets/{vs.name}", "target_not_found", stack=doc.stack
                )
                return
            kind, target_id, label = AttachmentTarget.stack, stack.id, stack.name
        else:
            env = self.envs.get((doc.stack or "", doc.environment or ""))
            if env is None:
                self.report.skip(
                    "attachment",
                    f"variable_sets/{vs.name}",
                    "target_not_found",
                    stack=doc.stack,
                    environment=doc.environment,
                )
                return
            kind, target_id = AttachmentTarget.environment, env.id
            label = f"{doc.stack}/{doc.environment}"
        path = f"variable_sets/{vs.name}/attachments/{doc.target}/{label}"
        await lock_attach_target(self.s, kind, target_id)  # no orphan on a racing deletion (§3.4)
        att = (
            await self.s.execute(
                select(VariableSetAttachment).where(
                    VariableSetAttachment.variable_set_id == vs.id,
                    VariableSetAttachment.target_kind == kind,
                    VariableSetAttachment.target_id == target_id,
                )
            )
        ).scalar_one_or_none()
        if att is None:
            self.s.add(
                VariableSetAttachment(
                    variable_set_id=vs.id,
                    target_kind=kind,
                    target_id=target_id,
                    priority=doc.priority,
                )
            )
            await self._audit(
                "variable_set.attached",
                "variable_set",
                vs.id,
                target_kind=kind.value,
                target_id=str(target_id),
                priority=doc.priority,
            )
            self.report.change("created", "attachment", path)
            return
        changed = _assign(att, {"priority": doc.priority})
        if changed:
            await self._audit(
                "variable_set.attachment_updated",
                "variable_set",
                vs.id,
                target_kind=kind.value,
                target_id=str(target_id),
                priority=doc.priority,
            )
        self.report.change("updated" if changed else "unchanged", "attachment", path, changed)

    # -- governance attached to a stack / an environment -----------------------------------------

    async def _hooks(
        self, kind: AttachmentTarget, target_id: uuid.UUID, docs: list[HookDoc], owner: str
    ) -> None:
        if not docs:
            return
        await lock_attach_target(self.s, kind, target_id)
        rows = (
            await self.s.execute(
                select(Hook).where(Hook.target_kind == kind, Hook.target_id == target_id)
            )
        ).scalars()
        existing: dict[tuple[Any, str], list[Hook]] = {}
        for h in rows:
            existing.setdefault((h.stage, h.name), []).append(h)
        for doc in docs:
            path = f"{owner}/hooks/{doc.stage.value}/{doc.name}"
            matches = existing.get((doc.stage, doc.name), [])
            values = {
                "command": doc.command,
                "on_failure": doc.on_failure,
                "position": doc.position,
            }
            if len(matches) > 1:
                self.report.skip("hook", path, "ambiguous_name")
                continue
            if not matches:
                hook = Hook(
                    target_kind=kind, target_id=target_id, stage=doc.stage, name=doc.name, **values
                )
                self.s.add(hook)
                await self.s.flush()
                await self._audit(
                    "hook.created",
                    "hook",
                    hook.id,
                    stage=doc.stage.value,
                    name=doc.name,
                    on=kind.value,
                )
                self.report.change("created", "hook", path)
                continue
            changed = _assign(matches[0], values)
            if changed:
                await self._audit("hook.updated", "hook", matches[0].id, fields=changed)
            self.report.change("updated" if changed else "unchanged", "hook", path, changed)

    async def _notifications(
        self,
        kind: AttachmentTarget,
        target_id: uuid.UUID,
        docs: list[NotificationTargetDoc],
        owner: str,
    ) -> None:
        if not docs:
            return
        await lock_attach_target(self.s, kind, target_id)
        existing: dict[str, list[NotificationTarget]] = {}
        for n in (
            await self.s.execute(
                select(NotificationTarget).where(
                    NotificationTarget.target_kind == kind,
                    NotificationTarget.target_id == target_id,
                )
            )
        ).scalars():
            existing.setdefault(n.name, []).append(n)
        for doc in docs:
            path = f"{owner}/notification_targets/{doc.name}"
            matches = existing.get(doc.name, [])
            if len(matches) <= 1 and (not matches or matches[0].url != doc.url):
                # The SSRF half the schema can't do (§14) — for a URL being written only: an
                # unchanged one was vetted when it was stored, and a transient DNS failure on it
                # must not abort the import.
                try:
                    await check_url_safe(doc.url)
                except ValueError as exc:
                    raise ProblemException(
                        422, "Unsafe notification URL", f"{path}: {exc}"
                    ) from exc
            values = {
                "kind": doc.kind,
                "url": doc.url,
                "on_states": list(doc.on_states),
                "enabled": doc.enabled,
            }
            if len(matches) > 1:
                self.report.skip("notification_target", path, "ambiguous_name")
                continue
            if not matches:
                target = NotificationTarget(
                    target_kind=kind, target_id=target_id, name=doc.name, **values
                )
                self.s.add(target)
                await self.s.flush()
                await self._audit(
                    "notification_target.created",
                    "notification_target",
                    target.id,
                    on=kind.value,
                    kind=doc.kind.value,
                    name=doc.name,
                )
                self.report.change("created", "notification_target", path)
                continue
            changed = _assign(matches[0], values)
            if changed:
                await self._audit(
                    "notification_target.updated",
                    "notification_target",
                    matches[0].id,
                    fields=changed,
                )
            self.report.change(
                "updated" if changed else "unchanged", "notification_target", path, changed
            )

    async def _cloud(self, env: Environment, doc: CloudIntegrationDoc, owner: str) -> None:
        path = f"{owner}/cloud_integration"
        values = doc.model_dump()
        ci = (
            await self.s.execute(
                select(CloudIntegration).where(CloudIntegration.environment_id == env.id)
            )
        ).scalar_one_or_none()
        same = ci is not None and all(getattr(ci, k) == v for k, v in values.items())
        if same:
            self.report.change("unchanged", "cloud_integration", path)
            return
        if not self.instance_admin:
            # Attaching IAM role ARNs is an instance admin's call (PUT …/cloud-integration, §10).
            self.report.skip("cloud_integration", path, "instance_admin_required")
            return
        created = ci is None
        if ci is None:
            ci = CloudIntegration(environment_id=env.id)
            self.s.add(ci)
        changed = _assign(ci, values)
        await self._audit(
            "cloud_integration.created" if created else "cloud_integration.updated",
            "environment",
            env.id,
        )
        self.report.change(
            "created" if created else "updated",
            "cloud_integration",
            path,
            None if created else changed,
        )

    # -- dependencies ----------------------------------------------------------------------------

    async def _foreign_env(self, space_name: str, stack: str, env: str) -> Environment | None:
        row = (
            await self.s.execute(
                select(Environment, Space)
                .join(Stack, Stack.id == Environment.stack_id)
                .join(Space, Space.id == Stack.space_id)
                .where(Space.name == space_name, Stack.name == stack, Environment.name == env)
            )
        ).first()
        if row is None:
            return None
        found: Environment = row[0]
        sp: Space = row[1]
        # Wiring a consumer of another space's outputs needs writer access there too (§3.8).
        await require_space_access(self.s, self.user, sp.id, min_role=Role.writer)
        return found

    async def _dependency(self, doc: DependencyDoc) -> None:
        down_key = (doc.downstream.stack, doc.downstream.environment)
        up = doc.upstream
        up_label = f"{up.space + ':' if up.space else ''}{up.stack}/{up.environment}"
        path = f"dependencies/{up_label}->{down_key[0]}/{down_key[1]}"
        down = self.envs.get(down_key)
        if down is None:
            self.report.skip("dependency", path, "downstream_not_found")
            return
        if up.space is None or up.space == self.space.name:
            upstream = self.envs.get((up.stack, up.environment))
        else:
            try:
                upstream = await self._foreign_env(up.space, up.stack, up.environment)
            except ProblemException:
                # Same answer as a missing one: an import must not reveal another space's names.
                self.report.skip("dependency", path, "upstream_not_found")
                return
        if upstream is None:
            self.report.skip("dependency", path, "upstream_not_found")
            return
        dep = (
            await self.s.execute(
                select(EnvDependency).where(
                    EnvDependency.upstream_env_id == upstream.id,
                    EnvDependency.downstream_env_id == down.id,
                )
            )
        ).scalar_one_or_none()
        if dep is None:
            try:
                await _create_edge(self.s, down.id, upstream.id, doc.trigger_policy, doc.references)
            except ProblemException as exc:
                raise ProblemException(exc.status, exc.title, f"{path}: {exc.detail}") from exc
            await self._audit(
                "dependency.created",
                "environment",
                down.id,
                upstream_env_id=str(upstream.id),
                references=len(doc.references),
            )
            self.report.change("created", "dependency", path)
            return
        changed = _assign(dep, {"trigger_policy": doc.trigger_policy})
        refs = {
            r.input_name: r
            for r in (
                await self.s.execute(
                    select(OutputReference).where(OutputReference.dependency_id == dep.id)
                )
            ).scalars()
        }
        for r in doc.references:
            row = refs.get(r.input_name)
            if row is None:
                self.s.add(
                    OutputReference(
                        dependency_id=dep.id,
                        output_name=r.output_name,
                        input_name=r.input_name,
                        mock_value=r.mock_value,
                    )
                )
                changed.append(f"references.{r.input_name}")
            elif _assign(row, {"output_name": r.output_name, "mock_value": r.mock_value}):
                changed.append(f"references.{r.input_name}")
        if changed:
            await self._audit(
                "dependency.updated",
                "environment",
                down.id,
                upstream_env_id=str(upstream.id),
                fields=changed,
            )
        self.report.change("updated" if changed else "unchanged", "dependency", path, changed)
