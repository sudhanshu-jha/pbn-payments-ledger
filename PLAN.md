# Tech Lead Plan — Idempotent Payment Ledger (Senior SWE take-home)

Source: "Payments Team — Senior Software Engineer (Assignment).pdf"
Time-box: 6–8 h of work, 7 calendar days to submit, 45-min follow-up with a live change.

---

## 0. What the reviewers are actually grading

Reading between the lines of the brief, the scoring axes are:

1. **Race-proofness, proven by tests** — the concurrent test "on separate DB connections" is the centrepiece. Everything else is table stakes.
2. **Ledger discipline** — append-only enforced at the *database*, status derivable from ledger alone.
3. **Boundary hygiene** — `payments` never touches `mock_processor` models; card data never reaches `payments`, DB, or logs.
4. **Judgement, written down** — conflicting terminal events, 10x volume, assumptions. The README is a graded artefact.
5. **Commit history** — they read it. Small, titled commits in build order.
6. **Live-change readiness** — "add refunds" or "a new failure code". Design so both are a 10-minute change.

Everything below is optimised for those six things. Anything that doesn't move one of them gets cut.

---

## 1. Architecture

```
┌──────────────┐  POST /processor/tokenize   ┌───────────────────┐
│  Client /    │ ───────────────────────────▶│  mock_processor   │  (treated as external)
│  React page  │                             │  - tokens          │
│              │  POST /api/payments         │  - charges         │
│              │ ─────────┐                  │  - simulate_webhooks
└──────────────┘          │                  └─────────┬─────────┘
                          ▼                            │ in-process call
                ┌───────────────────┐  charge(token) ◀─┘ via mock_processor.public ONLY
                │   payments        │ ─────────────────▶
                │  - Payment        │
                │  - LedgerEntry    │ ◀──── POST /webhooks/processor (HMAC-SHA256, X-Processor-Signature)
                │  - WebhookEvent   │
                │  - IdempotencyKey │
                └───────────────────┘
```

Two Django apps, one hard boundary:

- `mock_processor` — the "Stripe". Owns card/bank data (only last4 + scenario persisted), tokens, charges, webhook delivery.
- `payments` — our service. Imports exactly one thing from the processor: `mock_processor.public` (a small facade module exposing `charge()` and `request_redelivery()`). A test asserts no module under `payments/` imports `mock_processor.models`. Swapping to HTTP later is a one-class change behind `payments/gateway.py`.

### 1.1 Data model (`payments`)

| Model | Purpose | Key constraints |
|---|---|---|
| `Payment` | The aggregate. `id` UUID, `amount` (int cents, >0), `currency` ('USD'), `payment_token`, `method`, `last4`, `brand_or_bank_type`, `processor_reference` (unique, null until charged), `status` (denormalised cache), timestamps | `CheckConstraint(amount > 0)`, `UniqueConstraint(processor_reference)` |
| `LedgerEntry` | **Append-only** record of every state transition. `payment` FK, `sequence` (per-payment int), `from_status`, `to_status`, `event_type`, `failure_code`, `amount_delta` (signed cents), `webhook_event` FK nullable, `occurred_at`, `created_at` | `Unique(payment, sequence)`; **partial unique** `(payment) WHERE to_status IN ('succeeded','failed')` → at most one terminal entry per payment, enforced by Postgres |
| `WebhookEvent` | Inbox / dedup table. `event_id` **unique**, `processor_reference`, `payload` (JSON), `outcome` (`applied`, `duplicate`, `ignored_stale`, `conflict`), `received_at` | `Unique(event_id)` is the at-most-once guarantee |
| `IdempotencyKey` | `key` unique, `request_hash` (sha256 of canonical JSON), `response_status`, `response_body`, `payment` FK, `created_at` | `Unique(key)` |

Why a separate inbox table instead of putting `event_id` on the ledger: "a duplicate webhook writes exactly one ledger entry" and "late pending must not regress" are cleanest when *the ledger only ever contains real transitions*. The inbox records every verified delivery and what we decided to do with it, so ops can see duplicates/conflicts without polluting the ledger.

`status` on `Payment` is a cache. It is written in the same transaction as the ledger entry, under the same row lock. A `payments.ledger.derive_status(entries)` function is the source of truth; a test and a `verify_ledger` management command assert `Payment.status == derive_status(ledger)` for every row. (Alternative considered: no column, compute via subquery. Rejected: list queries get ugly for no safety gain, since the invariant is tested.)

`amount_delta` on ledger entries is a deliberate affordance for the live-change round: a refund becomes a new entry with a negative delta and the ledger sums to a balance. Costs nothing now.

### 1.2 State machine (`payments/state_machine.py`)

Pure function, no DB, fully unit-tested:

```
decide(current: Status, incoming: Status) -> Decision
  pending   + pending    -> IGNORE (no-op, already pending)
  pending   + succeeded  -> APPLY  (→ succeeded)
  pending   + failed     -> APPLY  (→ failed)
  terminal  + pending    -> IGNORE_STALE   (late pending; no regression)
  terminal  + same term. -> IGNORE_DUPLICATE_SEMANTIC (different event_id, same outcome)
  terminal  + other term.-> CONFLICT       (first terminal wins; recorded, alerted, no change)
```

**Conflicting terminals — decision: first terminal wins, second is recorded as `conflict` in the inbox, logged at ERROR, acknowledged with HTTP 200.**
Why: (a) "terminal states are final" is the brief's own rule, and honouring it unconditionally gives a ledger with exactly one terminal entry, which the partial unique index then enforces mechanically; (b) `occurred_at` is processor clock, unreliable under retries, so "latest wins" is not safer, just different; (c) a real "succeeded then failed" is a reversal/chargeback, which is a *new* event type with its own ledger entry, not a status flip — that's the refund extension, not a mutation of history. Returning 200 (not 4xx/5xx) because a real processor would otherwise retry forever; the conflict is our problem to investigate, not theirs to redeliver.

Reverse order is just `pending + succeeded → APPLY` followed by `succeeded + pending → IGNORE_STALE`. No special casing.

### 1.3 Concurrency and idempotency — the exact locks

**Webhook path** (`payments/services/webhooks.py::apply_event`):

```
verify_signature(raw_body, header)            # before any parsing, hmac.compare_digest
payload = json.loads(raw_body)
with transaction.atomic():
    inserted = WebhookEvent.insert_ignore(event_id, payload)   # INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING id
    if not inserted: return 200 {"outcome": "duplicate"}
    payment = Payment.objects.select_for_update().get(processor_reference=...)
    decision = decide(payment.status, payload["status"])
    if decision is APPLY:
        LedgerEntry.objects.create(... sequence=next, webhook_event=evt)
        payment.status = new; payment.save(update_fields=["status"])
    evt.outcome = decision.outcome; evt.save()
return 200 {"outcome": ...}
```

Two mechanisms, each covering a case the other doesn't:

- `UNIQUE(event_id)` handles *identical* deliveries, including the truly-concurrent case: in Postgres the second `INSERT` blocks on the unique index until the first transaction commits, then returns 0 rows. No application-level lock needed for this. This is what makes the "two copies at the same moment" test pass with one ledger entry.
- `SELECT … FOR UPDATE` on the payment row handles *different* events for the same payment racing (e.g. `succeeded` evt_1 and `failed` evt_2 at once). It serialises the decide-then-write so the second sees the first's status.
- Partial unique index on terminal ledger entries is the belt-and-braces: even if application code regresses, the DB refuses a second terminal row.

Why not `SERIALIZABLE`/advisory locks: row lock + unique index are cheaper, local to one payment (scales horizontally), and have no retry loop to get wrong.

**Create path** (`POST /api/payments`):

```
key = header or 400
hash = sha256(canonical_json(body))
with transaction.atomic():
    row, created = IdempotencyKey.insert_or_get(key)         # INSERT ... ON CONFLICT DO NOTHING; then SELECT ... FOR UPDATE
    if not created:
        if row.request_hash != hash: return 422 idempotency_key_reused
        if row.response_body is None: return 409 request_in_flight
        return row.response_status, row.response_body
    validate body (amount int > 0, currency == USD, token present)
    payment = Payment(status=pending) ; LedgerEntry(created, None→pending)
    result = gateway.charge(token, amount, currency)        # may raise ProcessorError
    payment.processor_reference = result.reference ; last4/brand/method from result
    row.request_hash = hash ; row.response = serialized ; row.payment = payment ; save
return 201
```

If the processor raises, we write `failed / processor_error` to the ledger and still store the 201-with-failed response under the key — the client gets an honest answer on retry rather than a second charge attempt. (Documented assumption.)

Key scope: global (brief has no auth requirement). Note in README that production scopes by `(merchant, key)` and expires keys after 24 h.

### 1.4 Append-only enforcement — three layers, the DB one is the real one

1. **Postgres trigger** (migration `RunSQL`): `BEFORE UPDATE OR DELETE ON payments_ledgerentry FOR EACH ROW EXECUTE FUNCTION raise_append_only()` which `RAISE EXCEPTION 'ledger is append-only'`. Survives raw SQL, admin, shell, bugs.
2. **ORM**: `LedgerEntry.save()` raises if `pk` already set; `delete()` raises; custom `QuerySet` with `update()`/`delete()`/`bulk_update()` overridden to raise. Catches mistakes early with a readable error.
3. **Tests**: attempt `.update()`, `.delete()`, `obj.save()` on existing, and raw `UPDATE`/`DELETE` via cursor — all must raise.

README states plainly: "the trigger is the enforcement; the ORM guards are ergonomics."

### 1.5 Webhook endpoint details

- Plain Django view (`csrf_exempt`), **not** a DRF view — we need `request.body` bytes untouched for HMAC and don't want DRF parsers/auth in the way.
- Signature: `hmac.new(secret, raw_body, sha256).hexdigest()`, compared with `hmac.compare_digest`. Missing/invalid → **401**, nothing written (not even inbox).
- Malformed JSON / unknown status → 400. Unknown `processor_reference` → 404, nothing written (a real processor would retry; documented).
- `PROCESSOR_WEBHOOK_SECRET` read from env via settings; `.env.example` carries a placeholder.

### 1.6 Card-data hygiene

- `mock_processor.tokenize` view decorated with `@sensitive_post_parameters()` and `@sensitive_variables()`; persists only `last4`, `method`, `brand_or_bank_type`, `scenario` (derived from last4 at tokenize time — the full number is discarded in the request handler).
- A logging `Filter` installed on the root logger that redacts any 13–19 digit run and any `cvv`/`cvc` key in `record.msg`/`args`. Defence in depth, documented as such.
- Payments API serializer has no card fields; unknown fields rejected (`serializer.Meta` + explicit check) so a client can't accidentally post `card_number` to us.
- Test: run full flow with PAN `4242424242424242`, CVV `987`, account `000123450341`; then (a) walk every table via `connection.introspection.table_names()` and assert the PAN/account strings appear nowhere, (b) capture all loggers at DEBUG with `assertLogs`/caplog and assert the same. CVV caveat in README: a 3-digit string can't be meaningfully asserted absent, so we assert the key is never persisted/logged and the tokenize view never stores it.

### 1.7 `mock_processor`

- `POST /processor/tokenize` — DRF view. Card: Luhn, expiry `MM/YY` not in past, CVV 3–4 digits. Bank: routing exactly 9 digits, account 4–17 digits. Errors: `invalid_number`, `invalid_expiry`, `invalid_cvv`, `invalid_routing_number`, `invalid_account_number` → 400 `{"error": code}`. Success → `{token, method: "card"|"bank", last4, brand_or_bank_type}` (brand from BIN prefix: 4→visa, 5→mastercard, 3→amex, else "unknown"; bank type "checking").
- Models: `ProcessorToken(token, method, last4, brand_or_bank_type, scenario, used_at)`, `ProcessorCharge(processor_reference, token FK, amount, currency, planned_status, planned_failure_code, delay_seconds, created_at)`.
- Scenario from last4: `0002` → failed (card_declined | insufficient_funds by method), `0119` → failed/processor_error, `0341` → succeeded after 15 s, else → succeeded.
- `public.charge(token, amount, currency) -> ChargeResult(processor_reference, method, last4, brand_or_bank_type)`; raises `InvalidToken` if unknown/used. Tokens single-use (documented; mirrors real processors).
- `public.request_redelivery(reference)` → re-sends the final event with its *original* `event_id`, so the replay endpoint can't break at-most-once.
- Event IDs deterministic: `evt_` + sha1(reference, status)[:16]. Duplicate/concurrent modes therefore carry identical `event_id` and identical signatures, as a real retry would.
- `simulate_webhooks --reference <ref> --mode normal|duplicate|reverse|concurrent [--fast] [--url URL]`
  - Builds `[pending, final]`, applies mode (duplicate: each twice; reverse: swap; concurrent: two threads + `threading.Barrier(2)` posting the final event), signs each, POSTs with `requests` to `settings.PROCESSOR_WEBHOOK_URL` (default `http://web:8000/webhooks/processor` in compose).
  - `0341`: sleeps 15 s between pending and final unless `--fast`.
  - Delivery is factored into `mock_processor/delivery.py::deliver(events, transport)` so tests can inject an in-process transport (Django test `Client`) and the command uses HTTP.

---

## 2. API contract

| Endpoint | Auth | Request | Responses |
|---|---|---|---|
| `POST /api/payments` | AllowAny (assumption) | header `Idempotency-Key` (required); `{amount:int>0, currency:"USD", payment_token}` | 201 payment; 200 same payment on replay; 400 missing key / validation; 409 in-flight; 422 `idempotency_key_reused` |
| `GET /api/payments/{id}` | AllowAny | — | 200 `{id, status, failure_code, amount, currency, method, last4, brand_or_bank_type, processor_reference_masked, created_at, ledger:[{sequence, from_status, to_status, event_type, failure_code, amount_delta, occurred_at, created_at}]}` |
| `GET /api/payments` | AllowAny | — | list (for React page) |
| `POST /api/payments/{id}/replay` | IsAdminUser | — | 202 if pending; 409 if terminal |
| `POST /webhooks/processor` | HMAC | contract from brief | 200 `{outcome}`; 401 bad sig; 400; 404 |
| `POST /processor/tokenize` | none | card or bank fields | 200 token; 400 `{error}` |

`processor_reference` is shown masked (`pr_…91c2`) in all API/UI output; full value only in DB.

---

## 3. Test plan (maps 1:1 to the brief's "Required tests")

Runner: the boilerplate uses Django's test runner via `make test` (`--keepdb --parallel`). Keep it; one command. The concurrent test needs real commits so it must be a `TransactionTestCase` and **must not** run under `--parallel` sharing state — mark it `serialized_rollback`-free and keep the suite small enough that parallel is fine, or run `make test` → `manage.py test` without `--parallel` (document the choice).

| # | Brief requirement | Test | Type |
|---|---|---|---|
| 1 | Same key + same body → same payment, nothing new | `test_idempotent_create_replays_response` asserts same id, `Payment.count()==1`, processor `charge` called once | TestCase |
| 2 | Same key + different body → rejected | `test_idempotent_create_rejects_different_body` → 422, count unchanged | TestCase |
| 3 | Duplicate webhook → one ledger entry | post same event twice → ledger count 1, inbox has `applied` + `duplicate` | TestCase |
| 4 | Reverse delivery → correct final status | final then pending → status succeeded, 1 transition entry, inbox `ignored_stale` | TestCase |
| 5 | **Truly concurrent** final events, separate connections, one state + one entry | `TransactionTestCase`; two threads each using Django test `Client`, `threading.Barrier(2)` before POST, `connection.close()` in `finally`; assert ledger count 1, both responses 200, outcomes = {applied, duplicate} | TransactionTestCase |
| 5b | Concurrent *different* terminals (succeeded vs failed, different event_ids) | same harness → exactly one terminal entry, one `conflict` in inbox | TransactionTestCase |
| 6 | Invalid / missing signature → rejected, nothing changes | 401, inbox count 0, ledger unchanged | TestCase |
| 7 | No PAN/CVV/account in DB or logs after full flow | full flow then scan all tables + captured logs | TestCase |
| 8 | Append-only | ORM update/delete/save raise; raw SQL UPDATE/DELETE raise `ledger is append-only` | TestCase |
| 9 | Status derivable from ledger | property test over random valid event sequences: `derive_status(ledger) == payment.status` | unit |
| 10 | State machine table | parametrised unit tests for every `(current, incoming)` cell | unit |
| 11 | Conflict policy | succeeded then failed → status stays succeeded, inbox `conflict`, ERROR log emitted | TestCase |
| 12 | Tokenize validation | Luhn fail, past expiry, 8-digit routing → correct error codes; success returns last4 only | TestCase |
| 13 | Boundary | AST/import scan: nothing in `payments/` imports `mock_processor.models` | unit |
| 14 | simulate_webhooks modes | each mode via in-process transport ends in correct status/entry count | TestCase |
| 15 | Replay endpoint | non-admin 403; pending payment → 202 and final applied once even if replayed twice | TestCase |

Test #5 is the one they'll read first. Make it obviously correct: comment the barrier, the connection handling, and why `TransactionTestCase`.

---

## 4. Work breakdown and commit sequence (≈7 h)

Each line is one commit (or two). Titles matter; they read the history.

| # | Commit | Est. |
|---|---|---|
| 0 | `chore: bootstrap vinta django-react-boilerplate (untouched)` — `django-admin startproject … --template=…zip`, `make docker_setup`, verify `make docker_up` + `make test` green. **Nothing else in this commit.** | 30 m |
| 1 | `feat(mock_processor): tokenize endpoint with Luhn/expiry/routing validation` + tests | 45 m |
| 2 | `feat(mock_processor): charges, scenario table, public facade (charge/request_redelivery)` | 30 m |
| 3 | `feat(payments): models, append-only ledger trigger, terminal partial unique index` + append-only tests | 45 m |
| 4 | `feat(payments): pure state machine + unit tests` | 20 m |
| 5 | `feat(payments): POST/GET /api/payments with Idempotency-Key` + tests 1, 2 | 60 m |
| 6 | `feat(payments): HMAC-verified webhook endpoint with inbox dedup` + tests 3, 4, 6, 11 | 60 m |
| 7 | `feat(mock_processor): simulate_webhooks command (normal/duplicate/reverse/concurrent)` + test 14 | 45 m |
| 8 | `test(payments): truly concurrent delivery on separate connections` (tests 5, 5b) | 45 m |
| 9 | `feat: sensitive-data log filter + no-card-data-anywhere test` (test 7) | 30 m |
| 10 | `docs: README setup, one-command tests, design notes, assumptions; .env.example` | 45 m |
| 11 | *(optional)* `feat(payments): admin-only replay endpoint` (test 15) | 30 m |
| 12 | *(optional)* `feat(frontend): read-only payments + ledger page (masked refs)` | 60 m |

Order rationale: 3→4→5→6 gets the graded core green by hour 4; the concurrent test (8) comes after the command (7) so it can reuse the delivery module; README last but budgeted, not squeezed.

Gate before submitting: fresh clone → `make docker_setup && make docker_up && make test` on a clean machine (or a clean Docker context). If that fails, nothing else matters.

---

## 5. README design notes — outline (keep to one page)

1. **State rules** — the transition table above; terminal is final; late pending ignored; conflicting terminal = first wins + recorded + alerted, and why (clock unreliability; reversals are new events).
2. **Idempotency & races** — `UNIQUE(event_id)` inbox for at-most-once; `SELECT FOR UPDATE` on payment for same-payment serialisation; partial unique index on terminal ledger entries as DB backstop; `UNIQUE(key)` + hash compare for create. One sentence each on why not advisory locks / SERIALIZABLE.
3. **Append-only** — Postgres trigger is the enforcement; ORM guards are ergonomics; tests prove both.
4. **Boundary & card data** — only `mock_processor.public` imported; what's persisted; log filter; the CVV caveat.
5. **At 10x volume** — webhook view inserts inbox row and returns 200 immediately, a worker applies events (`FOR UPDATE SKIP LOCKED` over the inbox), same locks, same code path; idempotency keys get a TTL + cleanup job; ledger partitioned by month; `GET` served from read replica; metrics + alerts on `conflict`/`ignored_stale` rates; processor client becomes HTTP with timeouts, retries with the *same* idempotency key, and a circuit breaker; webhook secret rotation by accepting two secrets; per-payment row locks already mean contention doesn't grow with volume, only with hot payments.
6. **Assumptions** — no auth on create/get (admin on replay); global key scope; tokens single-use; unknown reference → 404; 200 on stale/duplicate/conflict; `0341` really sleeps 15 s unless `--fast`; currency USD only; amounts > 0.

---

## 6. Interview prep (the 20-minute live change)

Pre-rehearse the two examples they name:

- **New failure code** (e.g. `expired_card`): one enum in `payments/enums.py` + one row in the processor scenario table + one test. Should take 5 minutes; if it takes longer the design is wrong.
- **Refunds**: new status `refunded` reachable from `succeeded`; new ledger `event_type=refund` with negative `amount_delta`; `POST /api/payments/{id}/refunds` with its own `Idempotency-Key`; processor `public.refund()`; webhook `status: refunded` flows through the *same* `apply_event` with one new row in the transition table. Mention the partial unique index needs `refunded` added or a separate index — good thing to say out loud.

Have running before the call: `make docker_up`, a tokenised 4242 payment, and `simulate_webhooks --mode concurrent` ready in a second terminal.

Questions to ask them (10 min): how they handle processor outages today (queue vs fail-fast), whether ledger is per-payment or double-entry in production, how they test concurrency in CI.

---

## 7. Risks and how they're handled

| Risk | Mitigation |
|---|---|
| Concurrent test flaky under `--parallel` / `--keepdb` | `TransactionTestCase`, barrier, explicit `connection.close()`; run suite without `--parallel` if needed and say so in README |
| DRF consumes `request.body` before HMAC | plain Django view for the webhook |
| Trigger migration breaks on SQLite | project is Postgres-only (boilerplate compose); state it; no SQLite fallback |
| Bootstrap commit accidentally includes generated files / secrets | `.env` in `.gitignore` (boilerplate does this); diff the first commit against the template zip |
| 15 s sleep slows demos/tests | `--fast` flag; tests always use the in-process transport with delay 0 |
| Time overrun on React page | it's optional; the backend tests are the deliverable; cut it first |
