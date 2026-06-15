# Notifications

The product's flow is `plan → human confirmation → apply`. Notifications make sure the human actually *learns* a decision is waiting — without staring at the UI.

A **notification target** is an outbound webhook attached to a **stack** or an **environment** (exactly like a platform hook). It fires when a run enters one of a chosen set of states.

## When notifications fire

| state | when it fires | typical use |
|---|---|---|
| `unconfirmed` | a plan is ready and **awaits confirmation** | ping the approver: "prod apply waiting" |
| `failed` | a run failed | alert on breakage |
| `finished` | a run finished (incl. apply) | deploy log / changelog |

Default `on_states` is `["unconfirmed", "failed"]`. Two kinds exist: `slack` posts `{"text": …}` to a Slack/Mattermost incoming webhook (with a deep link to the run), and `webhook` posts a structured JSON envelope (state, run id, stack/env, tier, commit, url). Deep links use `STACKD_APP_URL`, the SPA base.

## Setting up Slack

The `slack` kind posts to a **Slack incoming webhook** — a URL Slack gives you that drops a message into one channel. No bot token, no scopes.

1. Go to **api.slack.com/apps → Create New App → From scratch**, pick your workspace.
2. Open **Incoming Webhooks** and toggle it **On**.
3. **Add New Webhook to Workspace**, choose the target channel, **Allow**.
4. Copy the URL — it looks like `https://hooks.slack.com/services/T000/B000/XXXX`. That's the `url` you give Stackd.

[Mattermost](https://docs.mattermost.com/developer/webhooks-incoming.html) incoming webhooks accept the same `{"text": …}` payload — use the `slack` kind with the Mattermost URL.

!!! tip "Verify before you rely on it"
    After creating a target, hit **test** (UI) or `POST …/notifications/{id}/test` (API). It delivers a message **explicitly flagged as a test** (`🧪 … (test)` for Slack, `{"event":"notification.test","test":true}` for a webhook), so you can confirm the URL works without waiting for a real run — and no one mistakes it for a real event.

## Setting up a generic webhook

The `webhook` kind POSTs the JSON envelope (see [below](#a-generic-webhook-target)) to any HTTPS endpoint you control (a CI bridge, a Lambda URL, an internal service).

- **Method/format**: `POST`, `Content-Type: application/json`.
- **No signature**: outbound notification webhooks are **not** HMAC-signed (unlike *inbound* GitHub webhooks, which use `stacks.webhook_secret`). Treat the URL itself as the secret — use an unguessable path or a token your receiver checks — and prefer an endpoint reachable only over TLS.
- **Idempotency**: delivery is at-least-once (see [Delivery guarantees](#delivery-guarantees)); key on `run_id` + `state` so a retry is a no-op.

## Ping the approver when a prod plan is waiting

Attach a Slack target to a production environment. It pings on `unconfirmed` (something to approve) and on `failed`:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/notifications \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
    "name": "prod-approvals",
    "kind": "slack",
    "url": "https://hooks.slack.com/services/XXX/YYY/ZZZ",
    "on_states": ["unconfirmed", "failed"],
    "enabled": true
  }'
```

The Slack message carries a deep link straight to the run awaiting confirmation — see [Runs & approvals](runs-and-approvals.md).

## A generic webhook target

Attach a structured-envelope target to a whole stack (it covers every environment of that stack):

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK_ID/notifications \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
    "name": "ci-bridge",
    "kind": "webhook",
    "url": "https://ci.example.com/hooks/stackd",
    "on_states": ["finished", "failed"],
    "enabled": true
  }'
```

The `webhook` kind POSTs a JSON envelope:

```json
{
  "state": "finished",
  "run_id": "0190e...c3",
  "stack": "payments",
  "environment": "payments-prod",
  "tier": "prod",
  "commit": "a1b2c3d",
  "url": "http://localhost:5173/runs/0190e...c3"
}
```

## Managing targets

Both scopes expose the same CRUD. List or create on the scope, then patch or delete by target id:

```bash
curl -s localhost:8000/api/v1/stacks/$STACK_ID/notifications -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH localhost:8000/api/v1/environments/$ENV_ID/notifications/$TARGET_ID \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"enabled": false}'
curl -s -X DELETE localhost:8000/api/v1/stacks/$STACK_ID/notifications/$TARGET_ID -H "Authorization: Bearer $TOKEN"
```

For a run's env, both the env-level and the stack-level targets resolve and fire (filtered by `on_states`).

## Delivery guarantees

Notifications are not a fire-and-forget side effect. When a run transitions to a notify-worthy state, a row is inserted into the outbox **in the same DB transaction** as the state change — no HTTP in the request path. A background scheduler drains the outbox under an advisory lock (one replica at a time) and POSTs to the matching targets.

!!! note
    Delivery is **at-least-once**. The outbox is drained under a `pg_try_advisory_lock` with `SKIP LOCKED`, so two API replicas never double-send — but a receiver may still see a retry, so make handlers idempotent. A rolled-back transition never notifies (its outbox row rolls back too). Failed deliveries retry up to 5 times, then dead-letter and log; a flaky Slack never blocks or fails a run.

## See also

- [Runs & approvals](runs-and-approvals.md)
- [Concepts](../CONCEPTS.md)
