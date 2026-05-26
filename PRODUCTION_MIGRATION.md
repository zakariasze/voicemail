# Production Migration — Multi-Customer SMS on Supabase

This document captures the path from the **PoC SMS follow-up** (currently
implemented as a `threading.Timer` inside Flask) to a **production
multi-customer system** backed by Supabase Postgres with durable
queueing, retries, and an audit log.

> **Status:** planning document. None of this is built yet. The PoC in
> `call_handler.py::_maybe_schedule_followup_sms` is what's live.

---

## Why migrate

The PoC works for a demo with one or two calls at a time. It breaks for
real customers:

| PoC limitation | Production requirement |
|---|---|
| Timer dies if Flask restarts → SMS lost, no record | Durable queue: pending SMS survives any restart |
| One Twilio sub-account, one A2P 10DLC brand | Per-customer Twilio sub-accounts + per-brand registration |
| No retry on Twilio API errors | 3 attempts with exponential backoff before marking failed |
| `voicemail.db` is a single SQLite file on one box | Supabase Postgres, multi-tenant, with backups & PITR |
| No way to see "what's pending / what failed" | Dashboard view of queue + per-customer cost attribution |
| Single Flask process — no horizontal scaling | Multiple Flask workers + at-least-one worker process |
| Customer data and call logs co-mingled | Row-level `tenant_id` and Supabase RLS for isolation |

---

## Target architecture

```
            ┌─────────────────────────────────────────────────┐
            │              Supabase Postgres                   │
            │  ┌─────────┐  ┌──────────┐  ┌────────────────┐ │
            │  │ tenants │  │  calls   │  │  sms_messages  │ │
            │  └─────────┘  └──────────┘  └────────────────┘ │
            │       ▲             ▲              ▲             │
            └───────┼─────────────┼──────────────┼─────────────┘
                    │             │              │
        ┌───────────┴────┐  ┌─────┴──────┐  ┌────┴───────┐
        │  Flask webhook │  │  Scheduler │  │ SMS worker │
        │  (Twilio       │  │  (dialer)  │  │ (poll +    │
        │   /voice,      │  │            │  │  send)     │
        │   /status)     │  │            │  │            │
        └────────────────┘  └────────────┘  └────────────┘
                ▲                                   │
                │                                   ▼
        ┌───────┴─────────────────────────────────────────┐
        │              Twilio                              │
        │  per-tenant sub-account, per-brand A2P 10DLC    │
        └──────────────────────────────────────────────────┘
```

### Three processes, one database
- **Flask webhook** — receives `/voice` and `/status` from Twilio.
  Writes call rows and enqueues SMS jobs. Stateless; scale horizontally.
- **Scheduler** — the existing dialer loop, unchanged in shape.
- **SMS worker** — new process. Polls `sms_messages` for due jobs,
  sends via Twilio, writes back status. Can run as multiple replicas
  with row-level locking (`SELECT … FOR UPDATE SKIP LOCKED`).

---

## Schema additions

### `tenants` (new)
```sql
CREATE TABLE tenants (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                    TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'paused', 'disabled')),
  twilio_account_sid      TEXT NOT NULL,
  twilio_auth_token_enc   TEXT NOT NULL,   -- encrypted at rest
  twilio_from_number      TEXT NOT NULL,
  hubspot_api_key_enc     TEXT,
  hubspot_list_id         TEXT,
  a2p_brand_id            TEXT,            -- Twilio brand SID
  a2p_campaign_id         TEXT,            -- Twilio campaign SID
  voicemail_recording_url TEXT,
  sms_followup_body       TEXT,
  sms_followup_delay_s    INTEGER NOT NULL DEFAULT 15,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `calls` (extend existing)
Add `tenant_id UUID REFERENCES tenants(id) NOT NULL`.

Drop the `sms_sent_at` column on `calls` — replaced by joins to
`sms_messages.status`.

### `sms_messages` (new) — the durable queue + audit log
```sql
CREATE TABLE sms_messages (
  id                  BIGSERIAL PRIMARY KEY,
  tenant_id           UUID NOT NULL REFERENCES tenants(id),
  call_sid            TEXT NOT NULL,             -- one SMS per call_sid
  to_number           TEXT NOT NULL,
  body                TEXT NOT NULL,
  scheduled_for       TIMESTAMPTZ NOT NULL,      -- when it becomes due
  status              TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','sending','sent','failed','cancelled')),
  attempts            INTEGER NOT NULL DEFAULT 0,
  max_attempts        INTEGER NOT NULL DEFAULT 3,
  twilio_message_sid  TEXT,                      -- set on success
  last_error          TEXT,                      -- last Twilio error
  last_attempt_at     TIMESTAMPTZ,
  sent_at             TIMESTAMPTZ,
  locked_at           TIMESTAMPTZ,               -- worker lease
  locked_by           TEXT,                      -- worker id
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, call_sid)                   -- idempotency key
);

CREATE INDEX sms_messages_due_idx
  ON sms_messages (scheduled_for)
  WHERE status = 'pending';
```

The `UNIQUE (tenant_id, call_sid)` constraint is the idempotency
guarantee — Twilio retries of `/status` cannot create duplicate rows.

### Row-Level Security (RLS)
Each tenant gets its own Postgres role. RLS policies on `calls`,
`sms_messages`, and `tenants` filter by `tenant_id = current_setting
('app.current_tenant')::uuid`. The Flask app sets that GUC at the
start of each request based on the inbound webhook's tenant resolution.

---

## SMS worker — pseudo-code

```python
# sms_worker.py — runs as a separate process / container
WORKER_ID = f"worker-{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL_S = 3

def loop():
    while True:
        job = claim_one_due_job()
        if job is None:
            time.sleep(POLL_INTERVAL_S)
            continue
        send_and_record(job)

def claim_one_due_job():
    # Atomic claim with row-level lock. SKIP LOCKED lets multiple
    # workers run safely.
    return db.fetchrow("""
        UPDATE sms_messages
        SET status='sending',
            locked_at=NOW(), locked_by=$1,
            attempts=attempts+1,
            last_attempt_at=NOW(),
            updated_at=NOW()
        WHERE id = (
            SELECT id FROM sms_messages
            WHERE status='pending' AND scheduled_for <= NOW()
            ORDER BY scheduled_for
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *;
    """, WORKER_ID)

def send_and_record(job):
    tenant = load_tenant(job['tenant_id'])
    twilio = twilio_for(tenant)
    try:
        msg = twilio.messages.create(
            to=job['to_number'],
            from_=tenant['twilio_from_number'],
            body=job['body'],
        )
        db.execute("""
            UPDATE sms_messages
            SET status='sent', sent_at=NOW(),
                twilio_message_sid=$1, updated_at=NOW()
            WHERE id=$2
        """, msg.sid, job['id'])
    except TwilioRestException as exc:
        terminal = job['attempts'] >= job['max_attempts'] or _is_terminal(exc)
        new_status = 'failed' if terminal else 'pending'
        backoff = min(600, 30 * 2 ** (job['attempts'] - 1))  # 30s, 60s, 120s…
        db.execute("""
            UPDATE sms_messages
            SET status=$1, last_error=$2,
                scheduled_for = CASE WHEN $1='pending'
                                     THEN NOW() + ($3 || ' seconds')::interval
                                     ELSE scheduled_for END,
                updated_at=NOW()
            WHERE id=$4
        """, new_status, str(exc), backoff, job['id'])
```

### Why `SELECT … FOR UPDATE SKIP LOCKED`
Lets N workers run in parallel without stepping on each other. Each
worker takes one due job, marks it `sending`, and only it sees that
row until the update commits. Standard pattern for Postgres-backed
queues.

### Stuck-job sweeper
A second tiny loop reclaims jobs that have been `sending` for >5
minutes (a worker crashed mid-send): set them back to `pending` so
another worker picks them up. The `attempts` counter caps retries so
this can't loop forever.

---

## Per-customer Twilio + A2P 10DLC

Each customer needs:
1. **Twilio sub-account** (created via the master account's API).
   Stored in `tenants.twilio_account_sid` + encrypted token. Cost:
   none beyond per-message rates.
2. **Phone number(s)** purchased on that sub-account. ~$1.15/month
   each.
3. **A2P 10DLC brand** registered with their business EIN. **$4
   one-time** per brand. Required for US SMS deliverability —
   unregistered traffic is heavily filtered or blocked.
4. **A2P 10DLC campaign** registered for the use case (e.g.
   "Customer Care"). **$15 one-time vetting** + **~$1.50/month** for
   low-volume mixed. Different categories have different fees;
   sole-proprietor flows are cheaper but capped at 1k segments/day.

### Provisioning flow
- Add a "create customer" admin endpoint (or one-off script).
- Create sub-account → buy number → register brand → register campaign
  → link number to campaign → store IDs in `tenants` row.
- Whole flow takes minutes of API calls + 1–7 days of carrier
  verification before SMS works at full deliverability.

---

## Cost model

### Per-customer recurring (paid by you, billed back to customer)
| Item | Cost |
|---|---|
| Phone number rental | ~$1.15/month |
| A2P 10DLC campaign (low-volume mixed) | ~$1.50/month |
| **Total fixed** | **~$2.65/month/customer** |

### Per-customer one-time
| Item | Cost |
|---|---|
| A2P brand registration | $4 |
| A2P campaign vetting | $15 |
| **Total setup** | **$19/customer** |

### Per-message (variable)
| Item | Cost |
|---|---|
| Outbound call (~1 min) | ~$0.014 |
| Twilio AMD | ~$0.0075 |
| Outbound SMS (160-char segment) | ~$0.013 |
| **Per voicemail + SMS drop** | **~$0.035** |

### Supabase
- Free tier: enough for development.
- Pro: $25/month — required for production (backups, no project
  pausing, larger DB). One project can host all customers via RLS.

### Worker hosting (pick one)
- Supabase Edge Functions on cron: cheapest, but cron minimum
  granularity makes the "15s after call ends" timing imprecise
  (would float to ~1 min after call ends). Acceptable trade-off if
  the marketing copy doesn't promise "instant."
- Railway / Fly.io / Cloud Run: $5–20/month for one always-on worker.
  Best fit for the 15s timing.
- Same VM as Flask: free, no isolation. Fine for early production.

---

## Migration plan (incremental)

The PoC and production can co-exist while the migration is in flight.
Order of work:

1. **Stand up Supabase project.** Free tier. Create schema (`tenants`,
   `calls`, `sms_messages`). Apply RLS policies but keep them off
   initially.
2. **Backfill schema in code.** Add `tenant_id` everywhere. Hardcode a
   single default tenant for the existing single-customer deployment.
3. **Dual-write `sms_messages`.** Keep the Flask timer running, but
   also enqueue a row in `sms_messages` with `scheduled_for = NOW() +
   15s`. Verify rows look right.
4. **Build the worker.** Run in `--dry-run` mode against the
   `sms_messages` table — log what it *would* send, don't actually
   send. Compare against what the timer sent.
5. **Cut over.** Remove the timer; the worker is now the only sender.
   Production-by-day, observation-by-week.
6. **Multi-tenant flip.** Turn RLS on. Add the per-customer
   provisioning script. Onboard the second customer end-to-end.
7. **Dashboard.** Extend `dashboard.py` (or a new admin app) with a
   `sms_messages` view per tenant: pending / sent / failed counts,
   click into individual messages for body and error.

---

## Open questions for production

- **Auth on the dashboard.** Currently zero. Supabase Auth or
  Cloudflare Access in front of the Flask dashboard?
- **PII retention.** How long do we keep `body` text? Depends on
  customer contracts.
- **Opt-out handling.** Inbound `STOP` SMSes need a handler that
  marks the number as opted-out at the tenant level and prevents
  future sends — STCR compliance.
- **Quiet hours.** Per-customer timezone-aware "don't send after 9pm"
  rule. Adds a "earliest_send_at" computation when enqueueing.
- **Cost attribution.** Do customers see itemized billing, or
  flat-rate-per-seat? Affects whether we need usage logging beyond
  what `sms_messages` already provides.
