from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import outbox
from app.config import get_settings
from app.enums import AttachmentTarget
from app.logging import get_logger
from app.models.environment import Environment
from app.models.notification import NotificationOutbox, NotificationTarget
from app.models.run import Run
from app.models.stack import Stack
from app.observability.metrics import outbox_dead_letters

_log = get_logger("stackd.notifications")

_HTTP_TIMEOUT = 5.0
_DNS_TIMEOUT = 5.0
_DELIVERY_CONCURRENCY = 8  # target POSTs a drain has in flight at once
# One target's whole delivery — DNS, then every vetted address tried in turn, then the response.
_DELIVERY_BUDGET = 30.0

# SSRF guard: notification targets are operator-supplied URLs we POST to from inside the cluster,
# so they must point at a public host over TLS. We reject non-https schemes and any host that
# resolves to a private / loopback / link-local / reserved / multicast address — link-local also
# covers the cloud metadata endpoint (169.254.169.254), which we name explicitly for clarity.
_METADATA_IP = ipaddress.ip_address("169.254.169.254")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip_is_blocked(ip: IPAddress) -> bool:
    return (
        ip == _METADATA_IP
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def check_url_syntax(url: str) -> httpx.URL:
    """DNS-free part of the URL check: https scheme, a host, and — when the host is a literal IP —
    a public one. Cheap enough for request-body validators. Raises ValueError.

    Parsed with httpx, the parser `deliver` builds the request from, so the host we vet is the host
    we connect to."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"invalid url: {exc}") from exc
    if parsed.scheme != "https":
        raise ValueError("url must use the https scheme")
    if not parsed.host:
        raise ValueError("url must include a host")
    literal = _literal_ip(parsed.host)
    if literal is not None and _ip_is_blocked(literal):
        raise ValueError(f"url host {parsed.host!r} resolves to a non-public address")
    return parsed


async def check_url_safe(url: str) -> tuple[httpx.URL, list[IPAddress]]:
    """Full URL check: `check_url_syntax`, then a single DNS resolution — on the loop's executor and
    time-bounded, so a slow resolver never blocks the event loop — whose every address must be
    public. Returns the parsed URL and the vetted addresses. Raises ValueError.

    Resolution failure is *not* an error here and yields no addresses — an unresolvable host can't
    reach an internal target, and surfacing DNS state as a validation error would leak resolver
    behaviour; `deliver` then refuses to send.
    """
    parsed = check_url_syntax(url)
    host = parsed.raw_host.decode("ascii")
    literal = _literal_ip(host)
    if literal is not None:
        return parsed, [literal]
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
            _DNS_TIMEOUT,
        )
    except (socket.gaierror, TimeoutError):
        return parsed, []
    addrs = list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))
    for addr in addrs:
        if _ip_is_blocked(addr):
            raise ValueError(f"url host {parsed.host!r} resolves to a non-public address ({addr})")
    return parsed, addrs


async def ensure_url_safe(url: str, loc: tuple[str, ...] = ("body", "url")) -> None:
    """`check_url_safe` as an API answer: an unsafe URL is a 422 problem located at `loc`, exactly
    like a failed body validator — the one shape every route (and the config import, §22) uses."""
    from fastapi.exceptions import RequestValidationError

    try:
        await check_url_safe(url)
    except ValueError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": loc,
                    "msg": f"Value error, {exc}",
                    "input": url,
                    "ctx": {"error": exc},
                }
            ]
        ) from exc


# Short human label per state for the message subject line.
_LABEL = {
    "unconfirmed": "plan awaiting confirmation",
    "finished": "run finished",
    "failed": "run failed",
}


def run_url(run_id: object) -> str | None:
    """Deep link to the SPA run page — the one place the /runs/{id} route is spelled out. Used by
    outbound notifications and the VCS post-back alike."""
    settings = get_settings()
    base = settings.stackd_app_url or settings.stackd_public_url
    return f"{base.rstrip('/')}/runs/{run_id}" if base else None


def _render(
    target: NotificationTarget, to_state: str, run: Run, stack: Stack, env: Environment
) -> dict:
    label = _LABEL.get(to_state, to_state)
    scope = f"{stack.name}/{env.name}"
    url = run_url(run.id)
    if target.kind.value == "slack":
        line = f"*{scope}* — {label} ({to_state})"
        if url:
            line += f"\n<{url}|Open run>"
        return {"text": line}
    # Generic webhook envelope.
    return {
        "event": "run.state_changed",
        "state": to_state,
        "run_id": str(run.id),
        "run_type": run.type.value,
        "stack": stack.name,
        "environment": env.name,
        "tier": env.tier,
        "commit_sha": run.commit_sha,
        "url": url,
    }


def _matching_targets(
    targets: list[NotificationTarget], stack: Stack, env: Environment, to_state: str
) -> list[NotificationTarget]:
    """A run notifies its environment's targets plus its stack's, those firing on `to_state`."""
    return [
        t
        for t in targets
        if (
            (t.target_kind == AttachmentTarget.environment and t.target_id == env.id)
            or (t.target_kind == AttachmentTarget.stack and t.target_id == stack.id)
        )
        and to_state in (t.on_states or [])
    ]


async def deliver(target: NotificationTarget, body: dict) -> bool:
    # Re-validate at request time: DNS can change since create-time validation, so the stored URL
    # is not trusted on its own. Then connect only to the addresses that single lookup vetted —
    # letting httpx resolve the hostname again would reopen a DNS-rebinding window (public IP for
    # the check, internal IP for the POST). The request URL carries the vetted IP while the Host
    # header, TLS SNI and certificate verification stay on the original hostname. The explicit
    # transport keeps httpx off env proxies: they'd re-resolve the hostname themselves (and
    # httpcore's proxy tunnel ignores sni_hostname). follow_redirects stays off so a public URL
    # can't 302 us onto an internal host.
    url, addrs = await check_url_safe(target.url)
    headers = {"Host": url.netloc.decode("ascii")}
    extensions = {"sni_hostname": url.raw_host.decode("ascii")}
    error: Exception = ValueError(f"url host {url.host!r} does not resolve")
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(), timeout=_HTTP_TIMEOUT, follow_redirects=False
    ) as http:
        for addr in addrs:
            try:
                resp = await http.post(
                    url.copy_with(host=str(addr)), json=body, headers=headers, extensions=extensions
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                error = exc  # unreachable — try the host's next vetted address
                continue
            resp.raise_for_status()
            return True
    raise error


@dataclass
class _Row:
    id: uuid.UUID
    run_id: uuid.UUID
    to_state: str
    attempt: int
    delivered: set[uuid.UUID]
    done: bool = False
    retry_after: float | None = None
    failed_targets: list[uuid.UUID] = field(default_factory=list)


async def dispatch_pending(session: AsyncSession, now: datetime, *, limit: int = 20) -> int:
    """Drain the due rows of the notification outbox, never holding a DB transaction across
    external HTTP I/O (§7.5).

    Phase 1 (locked, brief): claim a batch of due rows with FOR UPDATE SKIP LOCKED, bump `attempts`
    and push `next_attempt_at` by the backoff, commit — this releases the row locks. Then one read
    of the batch's runs/envs/stacks and their targets, committed too. Phase 2 (no DB transaction):
    POST to each row's matching targets not delivered yet — at most `_DELIVERY_CONCURRENCY` at
    once, one run's rows in order. Phase 3: record the delivered targets, mark the fully delivered
    rows `sent_at`, push a throttled row past its Retry-After, report the rows out of attempts.
    A crash between phases leaves the row due again after its backoff (at-least-once); the
    advisory lock keeps the drain single across replicas."""
    max_attempts = get_settings().stackd_outbox_max_attempts
    claimed = (
        (
            await session.execute(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.sent_at.is_(None),
                    NotificationOutbox.attempts < max_attempts,
                    NotificationOutbox.next_attempt_at <= now,
                )
                .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not claimed:
        return 0

    rows: list[_Row] = []
    for r in claimed:
        r.attempts += 1
        r.next_attempt_at = outbox.next_attempt_at(now, r.attempts)
        rows.append(_Row(r.id, r.run_id, r.to_state, r.attempts, set(r.delivered_target_ids)))
    await session.commit()

    triples = (
        await session.execute(
            select(Run, Environment, Stack)
            .join(Environment, Environment.id == Run.environment_id)
            .join(Stack, Stack.id == Environment.stack_id)
            .where(Run.id.in_({r.run_id for r in rows}))
        )
    ).all()
    by_run = {run.id: (run, env, stack) for run, env, stack in triples}
    targets = list(
        (
            await session.execute(
                select(NotificationTarget)
                .where(
                    NotificationTarget.enabled.is_(True),
                    or_(
                        and_(
                            NotificationTarget.target_kind == AttachmentTarget.environment,
                            NotificationTarget.target_id.in_({e.id for _, e, _ in triples}),
                        ),
                        and_(
                            NotificationTarget.target_kind == AttachmentTarget.stack,
                            NotificationTarget.target_id.in_({s.id for _, _, s in triples}),
                        ),
                    ),
                )
                .order_by(NotificationTarget.created_at)
            )
        )
        .scalars()
        .all()
    )
    # End the read transaction before any HTTP. Commit, not rollback (which would expire the
    # loaded rows): with expire_on_commit=False they stay usable — plain columns only.
    await session.commit()

    bound = asyncio.Semaphore(_DELIVERY_CONCURRENCY)

    async def _send(row: _Row, target: NotificationTarget, body: dict[str, Any]) -> bool:
        try:
            async with bound, asyncio.timeout(_DELIVERY_BUDGET):
                await deliver(target, body)
        except Exception as exc:  # external endpoint — never crash the drain
            retry_after = outbox.retry_after_seconds(exc, datetime.now(UTC))
            if retry_after is not None:
                row.retry_after = max(row.retry_after or 0.0, retry_after)
            row.failed_targets.append(target.id)
            _log.warning(
                "notification delivery failed",
                extra={
                    "event": "notification.failed",
                    "target_id": str(target.id),
                    "run_id": str(row.run_id),
                    "state": row.to_state,
                    "attempt": row.attempt,
                    "error": str(exc) or type(exc).__name__,
                },
            )
            return False
        return True

    async def _deliver_row(row: _Row) -> None:
        if row.run_id not in by_run:
            row.done = True  # nothing to notify — mark done
            return
        run, env, stack = by_run[row.run_id]
        pending = [
            t
            for t in _matching_targets(targets, stack, env, row.to_state)
            if t.id not in row.delivered
        ]
        sent = await asyncio.gather(
            *(_send(row, t, _render(t, row.to_state, run, stack, env)) for t in pending)
        )
        row.delivered |= {t.id for t, ok in zip(pending, sent, strict=True) if ok}
        row.done = all(sent)
        if row.done and pending:
            _log.info(
                "notifications delivered",
                extra={
                    "event": "notification.delivered",
                    "run_id": str(row.run_id),
                    "state": row.to_state,
                    "targets": len(pending),
                },
            )

    by_run_rows: dict[uuid.UUID, list[_Row]] = {}
    for row in rows:
        by_run_rows.setdefault(row.run_id, []).append(row)

    async def _deliver_run(run_rows: list[_Row]) -> None:
        for row in run_rows:  # one run's events reach a target in the order they happened
            await _deliver_row(row)

    await asyncio.gather(*(_deliver_run(run_rows) for run_rows in by_run_rows.values()))

    # Phase 3: persist the outcome.
    for row in rows:
        values: dict[str, Any] = {"delivered_target_ids": sorted(row.delivered)}
        if row.done:
            values["sent_at"] = now
        elif row.attempt >= max_attempts:
            outbox_dead_letters.labels(outbox="notification").inc()
            _log.error(
                "notification abandoned after its last attempt",
                extra={
                    "event": "notification.dead_lettered",
                    "outbox_id": str(row.id),
                    "run_id": str(row.run_id),
                    "state": row.to_state,
                    "attempts": row.attempt,
                    "failed_target_ids": [str(t) for t in row.failed_targets],
                },
            )
        elif row.retry_after is not None:
            values["next_attempt_at"] = outbox.next_attempt_at(
                datetime.now(UTC), row.attempt, row.retry_after
            )
        await session.execute(
            update(NotificationOutbox).where(NotificationOutbox.id == row.id).values(**values)
        )
    await session.commit()
    return len(rows)
