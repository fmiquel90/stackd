from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import AttachmentTarget
from app.logging import get_logger
from app.models.environment import Environment
from app.models.notification import NotificationOutbox, NotificationTarget
from app.models.run import Run
from app.models.stack import Stack

_log = get_logger("stackd.notifications")

_MAX_ATTEMPTS = 5  # after this, the row is dead-lettered (left unsent, excluded from the poll)
_HTTP_TIMEOUT = 5.0
_DNS_TIMEOUT = 5.0

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


async def _matching_targets(
    session: AsyncSession, stack: Stack, env: Environment, to_state: str
) -> list[NotificationTarget]:
    rows = (
        (
            await session.execute(
                select(NotificationTarget).where(
                    NotificationTarget.enabled.is_(True),
                    (
                        (NotificationTarget.target_kind == AttachmentTarget.environment)
                        & (NotificationTarget.target_id == env.id)
                    )
                    | (
                        (NotificationTarget.target_kind == AttachmentTarget.stack)
                        & (NotificationTarget.target_id == stack.id)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return [t for t in rows if to_state in (t.on_states or [])]


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


async def dispatch_pending(session: AsyncSession, now: datetime, *, limit: int = 20) -> int:
    """Drain the notification outbox without holding a DB transaction across external HTTP I/O.

    Phase 1 (locked, brief): claim a batch with FOR UPDATE SKIP LOCKED, bump `attempts`, commit —
    this releases the row locks immediately. Phase 2 (no DB txn held): POST to matching targets.
    Phase 3: mark the delivered rows `sent_at`. A crash between phases leaves `attempts` bumped
    and `sent_at` null → retried later (at-least-once); the advisory lock keeps it single."""
    rows = (
        (
            await session.execute(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.sent_at.is_(None),
                    NotificationOutbox.attempts < _MAX_ATTEMPTS,
                )
                .order_by(NotificationOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Phase 1: snapshot what we need, bump attempts, release the locks.
    batch = [(r.id, r.run_id, r.to_state, r.attempts + 1) for r in rows]
    for r in rows:
        r.attempts += 1
    await session.commit()

    # Phase 2: deliver with no DB transaction open.
    delivered: list[uuid.UUID] = []
    for row_id, run_id, to_state, attempt in batch:
        run = await session.get(Run, run_id)
        env = await session.get(Environment, run.environment_id) if run else None
        stack = await session.get(Stack, env.stack_id) if env else None
        if run is None or env is None or stack is None:
            delivered.append(row_id)  # nothing to notify — mark done
            continue
        targets = await _matching_targets(session, stack, env, to_state)
        ok = True
        for target in targets:
            try:
                await deliver(target, _render(target, to_state, run, stack, env))
            except Exception as exc:  # external endpoint — never crash the loop
                ok = False
                _log.warning(
                    "notification delivery failed",
                    extra={
                        "event": "notification.failed",
                        "target_id": str(target.id),
                        "run_id": str(run_id),
                        "state": to_state,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
        if ok:
            delivered.append(row_id)
            if targets:
                _log.info(
                    "notifications delivered",
                    extra={
                        "event": "notification.delivered",
                        "run_id": str(run_id),
                        "state": to_state,
                        "targets": len(targets),
                    },
                )

    # Phase 3: mark the delivered rows done.
    if delivered:
        await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id.in_(delivered))
            .values(sent_at=now)
        )
        await session.commit()
    return len(batch)
