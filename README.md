# pbn-payments-ledger — Idempotent Payment Ledger (Senior SWE take-home)

A small payments service that models a payment's lifecycle on an **append-only
ledger** and proves with tests that duplicate, concurrent and out-of-order
processor webhooks can never double-process a payment or corrupt its ledger.

Built on [Vinta's Django React Boilerplate](https://github.com/vintasoftware/django-react-boilerplate)
(Django 5 + DRF + React + Postgres). The first commit is the untouched
boilerplate; everything after it is the assignment.

```
┌──────────────┐  POST /processor/tokenize    ┌────────────────────┐
│ client /     │ ────────────────────────────▶│  mock_processor    │  "Stripe"
│ React page   │                              │  tokens, charges,  │
│              │  POST /api/payments          │  simulate_webhooks │
│              │ ───────┐  (token only)       └─────────┬──────────┘
└──────────────┘        ▼                               │ in-process, via
              ┌──────────────────────┐   charge()       │ mock_processor.public only
              │  payments            │◀─────────────────┘
              │  Payment             │
              │  LedgerEntry (append-only, DB trigger)
              │  WebhookEvent (inbox, UNIQUE event_id)   ◀── POST /webhooks/processor (HMAC-SHA256)
              │  IdempotencyKey                          │
              └──────────────────────┘
```

---

## Quick start (Docker Compose)

Prerequisites: Docker Desktop (or Docker Engine + Compose v2), `make`.

```bash
cp backend/.env.example backend/.env       # placeholders only; see "Secrets" below
make docker_setup                           # builds images, generates the OpenAPI schema + TS client
make docker_migrate
make docker_up                              # http://localhost:8000
```

### Run the full test suite (one command)

```bash
make docker_test
```

Equivalent without `make`: `docker compose run --rm backend python manage.py test --parallel --keepdb`.
The suite runs against the real Postgres container; it does not support SQLite
(row locks, the append-only trigger and the partial unique index are Postgres features).

### Secrets

Nothing secret is committed. `backend/.env.example` carries placeholders for

| variable | purpose |
|---|---|
| `PROCESSOR_WEBHOOK_SECRET` | HMAC-SHA256 key shared with the processor. Empty = every webhook is rejected (fail closed). |
| `PROCESSOR_WEBHOOK_URL` | Where `simulate_webhooks` delivers. Inside compose the backend is reachable as `http://backend:8000`. |

### Without Docker

Needs Python 3.12, Poetry, Node 22 + pnpm, and a local Postgres.

```bash
cp backend/.env.example backend/.env       # set DATABASE_URL=postgres://... and PROCESSOR_WEBHOOK_URL=http://localhost:8000/webhooks/processor
cp backend/pbn_payments/settings/local.py.example backend/pbn_payments/settings/local.py
poetry install --with dev --no-root
cd backend && poetry run python manage.py migrate && poetry run python manage.py runserver
# tests
make test            # == poetry run backend/manage.py test backend/ --parallel --keepdb
```

---

## Walkthrough

Everything below works against `http://localhost:8000` once `make docker_up` is running.

**1. Tokenize** (this is the processor; card data goes here and only here):

```bash
curl -s -X POST localhost:8000/processor/tokenize -H 'Content-Type: application/json' \
  -d '{"number":"4242 4242 4242 4242","expiry":"12/30","cvv":"123"}'
# {"token":"tok_…","method":"card","last4":"4242","brand_or_bank_type":"visa"}
```

Bank: `{"account_number":"000123456789","routing_number":"021000021"}`.
Errors: `invalid_number` (Luhn), `invalid_expiry`, `invalid_cvv`, `invalid_routing_number`, `invalid_account_number`.

**2. Create a payment** (token only, amount in cents, `Idempotency-Key` required):

```bash
curl -s -X POST localhost:8000/api/payments -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-1234' \
  -d '{"amount":1250,"currency":"USD","payment_token":"tok_…"}'
# 201 {"id":"…","status":"pending", … ,"processor_reference":"pr_…efcb","ledger":[{"sequence":1,"to_status":"pending",…}]}
```

Repeat the same request → same `201` body, header `Idempotent-Replayed: true`, nothing created.
Same key, different body → `422 {"error":"idempotency_key_reused"}`.

**3. Deliver the processor's webhooks, messily.** Find the full `processor_reference`
(the API only ever shows it masked) and pick a mode:

```bash
docker compose run --rm backend python manage.py simulate_webhooks --reference pr_… --mode concurrent
#   evt_…pending:   HTTP 200 {"outcome": "ignored_redundant", …}
#   evt_…succeeded: HTTP 200 {"outcome": "applied",   "status": "succeeded"}
#   evt_…succeeded: HTTP 200 {"outcome": "duplicate", "status": "succeeded"}
```

| mode | delivery |
|---|---|
| `normal` | pending, then final |
| `duplicate` | every event twice, back to back |
| `reverse` | final first, then pending (→ `ignored_stale`) |
| `concurrent` | pending, then **two copies of the final event released at the same instant** from two threads behind a barrier |

Add `--fast` to skip the 15 s settlement wait for the `0341` test values. The command
posts over real HTTP to `PROCESSOR_WEBHOOK_URL`; every delivery carries
`X-Processor-Signature`.

**4. Read it back**: `GET /api/payments/{id}` returns the current status plus the full ledger,
and **http://localhost:8000/payments** is a read-only React page listing payments with each
one's ledger history (masked references only).
The admin (`/admin/`, create a superuser with `docker compose run --rm backend python manage.py createsuperuser`)
shows payments, ledger entries, the webhook inbox and idempotency keys, all read-only.

Test values (last 4 digits decide the outcome): `0002` → `failed/card_declined`
(bank: `insufficient_funds`), `0119` → `failed/processor_error`, `0341` → pending 15 s then
`succeeded`, anything else → `succeeded`. `4242 4242 4242 4242` is a valid success card.

### Endpoints

| endpoint | auth | notes |
|---|---|---|
| `POST /processor/tokenize` | none | processor side; raw card/bank data in, token out |
| `POST /api/payments` | none (assumption) | `Idempotency-Key` header required |
| `GET /api/payments/{id}` | none | status + ledger history, masked reference |
| `GET /api/payments` | none | list, newest first (for the UI) |
| `POST /api/payments/{id}/replay` | admin session | re-requests a stuck payment's result (optional feature) |
| `POST /webhooks/processor` | HMAC signature | `200 {outcome}`, `401` bad/missing signature, `400` malformed, `404` unknown reference |

---

## Design notes

### State rules

```
pending ──► succeeded
pending ──► failed          terminal states are final
```

`payments/state_machine.py::decide(current, incoming)` is a pure function with one
row per cell, unit-tested cell by cell:

| current \ incoming | `pending` | same terminal | other terminal |
|---|---|---|---|
| `pending` | ignored (redundant) | **applied** | **applied** |
| terminal | ignored (**stale**: a late pending never regresses) | ignored (redundant, new event_id) | **conflict** |

**Conflicting terminals (`succeeded` then `failed`): first wins.** The second
delivery is recorded in the inbox with `outcome=conflict`, logged at ERROR, and
acknowledged with `200` so a real processor stops retrying. Why first-wins rather
than latest-`occurred_at`-wins: `occurred_at` is the processor's clock on a
retried, reordered stream and is not a safe tiebreaker; "terminal is final" gives
a ledger with exactly one terminal row, which the database then enforces
mechanically; and a genuine "succeeded, later failed" is a reversal or dispute, a
*new* event type with its own ledger entry (see refunds below), not a rewrite of
history. Reverse-order delivery needs no special casing: the final event applies
directly from `pending`, and the late `pending` is stale.

### Idempotency and races: which locks and constraints, and why

**Webhooks** (`payments/services/webhooks.py::apply_event`), inside one transaction:

1. `SELECT … FOR UPDATE` on the payment row. Serialises every event for the same
   payment, so decide-then-write can't interleave between two *different* events
   (e.g. `succeeded` and `failed` arriving at once with different ids).
2. `INSERT` into `WebhookEvent` with `UNIQUE(event_id)`. For an *identical*
   delivery (retry, or two copies at the same instant) the second insert blocks
   on the unique index until the first transaction commits, then fails on a
   savepoint → `outcome=duplicate`, `delivery_count += 1`, no ledger write.
3. Only then the state machine decides and, if it applies, a `LedgerEntry` is
   written and `Payment.status` refreshed, in the same transaction as the inbox row.

Backstop: a **partial unique index** `(payment) WHERE to_status IN ('succeeded','failed')`
means the database itself refuses a second terminal ledger row even if application
code regresses. Why not `SERIALIZABLE` or advisory locks: the row lock + unique
index are cheaper, need no retry loop, and are scoped to one payment, so contention
grows with hot payments, not with volume.

**Create** (`payments/services/payments.py`): `UNIQUE(key)` on `IdempotencyKey` is
the race guard (a concurrent duplicate blocks on the index, then sees the committed
row). The request body is hashed as canonical JSON; same hash → the stored
`(status, body)` is replayed verbatim; different hash → `422`. Payment creation and
the processor charge run in a savepoint inside that transaction: a token the
processor rejects rolls back (no orphan payment), while a processor *outage* keeps
the payment and records `failed/processor_error`, so the client gets an honest
answer and a retry replays it instead of charging twice. Keys are global (no auth
in the brief); production would scope by merchant and expire after 24 h.

**Concurrency is proven, not asserted**: `payments/tests/test_concurrency.py` uses
`TransactionTestCase` with worker threads released by a `threading.Barrier`, each on
its own Postgres connection, posting through the real HTTP handler. Identical final
event ×2 and ×5, conflicting terminals at the same instant, pending vs final racing,
and two creates with the same key all end in one correct state with one ledger entry.

### Append-only ledger: how it is enforced

1. **Postgres trigger** (`payments/migrations/0002`): `BEFORE UPDATE OR DELETE ON
   payments_ledgerentry … RAISE EXCEPTION`. This is the guarantee: it stops the ORM,
   raw SQL, the admin and `psql` alike. `TRUNCATE` is intentionally not blocked so the
   test runner can reset tables.
2. **ORM guards** (`payments/models.py`): `LedgerEntry.save()` on an existing row,
   `.delete()`, and `QuerySet.update()/delete()/bulk_update()` raise
   `LedgerImmutableError` with a readable message before the database does.
3. `Payment` → `LedgerEntry` is `on_delete=PROTECT`, so a payment can't cascade its
   history away. `test_ledger_append_only.py` exercises all of the above, including
   raw `UPDATE`/`DELETE`.

`Payment.status` is a cache of the newest ledger row's `to_status`, written only under
the row lock in the same transaction. `payments/ledger.py::derive_status()` is the
source of truth; `manage.py verify_ledger` checks every payment against it (and a
test shows it catching deliberate drift).

### Boundary and card data

`payments` imports exactly one thing from the processor: `mock_processor.public`
(`charge()`, `request_redelivery()`), and only from `payments/gateway.py`.
`test_boundary.py` enforces this with an AST scan and checks no payments model relates
to a processor table. The processor persists only `last4`, `method`, brand and the
pre-decided outcome; the raw number and CVV are discarded in the tokenize request.
The payments API rejects unknown fields, so a client can't post a PAN to it by
accident. A logging filter (`payments/logging_filters.py`) redacts 9–19 digit runs and
CVV fields on every handler as defence in depth; `test_sensitive_data.py` runs a full
card + bank flow with distinctive numbers and then scans **every table** and the
**raw, pre-filter log records** to prove nothing sensitive was stored or logged.
(CVV caveat: a three-digit string can't be meaningfully asserted absent, so the test
asserts the field name is never logged and the processor never stores it.)

### What I'd change at 10× volume

- **Ack fast, apply async.** The webhook view would verify the signature, insert the
  inbox row and return `200`; a worker pool would apply events with
  `SELECT … FOR UPDATE SKIP LOCKED` over the inbox, through the *same* `apply_event`
  and the same locks. At-most-once still rests on `UNIQUE(event_id)`.
- Idempotency keys get a TTL and a cleanup job; the ledger is partitioned by month;
  `GET` is served from a read replica.
- The processor client becomes HTTP with timeouts, retries that reuse the *same*
  idempotency key, and a circuit breaker; webhook secret rotation by accepting two secrets.
- Metrics and alerts on `conflict` and `ignored_stale` rates: both are "the processor
  and we disagree" signals.
- Per-payment row locks already mean contention scales with hot payments, not with
  throughput, so the locking design stays.

### Extending it (e.g. refunds, new failure codes)

Ledger entries carry a signed `amount_delta` (captures are `+amount`), so the ledger
sums to a balance. A refund is: a new status reachable from `succeeded`, a new
`event_type` with a negative delta, one new row in the transition table, and
`public.refund()` on the processor. A new failure code is one entry in
`payments/enums.py` plus one row in `mock_processor/scenarios.py`.

---

## Assumptions and deviations

- **No authentication** on `POST/GET /api/payments` (the brief doesn't ask for it);
  `replay` requires an admin session. Idempotency keys are therefore global.
- **Processor tokens are single-use**, like real processors; charging a used or unknown
  token → `422 invalid_payment_token`, and that response is stored under the key too.
- Webhooks for an **unknown `processor_reference` → 404** and nothing is written, so a
  real processor would retry until our side has the charge.
- Stale, redundant and conflicting deliveries are **acknowledged with 200** (recorded in
  the inbox); non-2xx would only make the processor retry forever.
- The pending webhook after creation is reported as `ignored_redundant`: the payment is
  already `pending` from entry #1, so it is not a state change and gets no ledger row.
- The `0341` wait is a real 15 s sleep in `simulate_webhooks`; `--fast` skips it, and
  tests use an in-process transport with no delay.
- Currency is `USD` only; amounts are positive integers (cents), also enforced by a
  DB check constraint.
- Signature: lowercase hex HMAC-SHA256 over the exact request bytes, compared with
  `hmac.compare_digest`. The webhook view is a plain Django view (not DRF) so nothing
  parses the body before verification.
- **Boilerplate fixes**: `postgres:alpine` is pinned to `postgres:16-alpine` (the
  unpinned 18 image refuses the compose file's data mount layout); `manage.py test`
  now *forces* the test settings module (inside compose the env file otherwise wins);
  CI runs against a Postgres service instead of SQLite; `globals` was added as a dev
  dependency because `eslint.config.mjs` imports it but the boilerplate never declared it.
