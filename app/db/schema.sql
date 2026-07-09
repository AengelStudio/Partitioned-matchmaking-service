CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,

    max_tickets_per_second INT NOT NULL DEFAULT 20,
    max_tickets_in_flight INT NOT NULL DEFAULT 50,
    max_partition_depth INT NOT NULL DEFAULT 1000,

    callback_url TEXT,
    callback_secret TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'ticket_rate_limit_per_minute'
    ) THEN
        ALTER TABLE tenants RENAME COLUMN ticket_rate_limit_per_minute TO max_tickets_per_second;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'max_waiting_tickets'
    ) THEN
        ALTER TABLE tenants RENAME COLUMN max_waiting_tickets TO max_tickets_in_flight;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    player_id TEXT NOT NULL,

    region TEXT NOT NULL,
    queue_name TEXT NOT NULL,
    skill INT NOT NULL,

    partition_id INT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('waiting', 'reserved', 'matched', 'cancelled')
    ),

    reserved_by TEXT,
    reserved_until TIMESTAMPTZ,

    match_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    matched_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tickets_partition_status_created
ON tickets (partition_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_tickets_tenant_status
ON tickets (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_tickets_player_lookup
ON tickets (tenant_id, player_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_ticket_per_player_queue
ON tickets (tenant_id, player_id, region, queue_name)
WHERE status IN ('waiting', 'reserved');

CREATE TABLE IF NOT EXISTS matches (
    match_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    region TEXT NOT NULL,
    queue_name TEXT NOT NULL,
    partition_id INT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('created', 'callback_pending', 'callback_delivered', 'callback_failed')
    ) DEFAULT 'created',

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tickets
DROP CONSTRAINT IF EXISTS fk_tickets_match;

ALTER TABLE tickets
ADD CONSTRAINT fk_tickets_match
FOREIGN KEY (match_id) REFERENCES matches(match_id);

CREATE TABLE IF NOT EXISTS match_players (
    match_id UUID NOT NULL REFERENCES matches(match_id),
    ticket_id UUID NOT NULL REFERENCES tickets(ticket_id),
    player_id TEXT NOT NULL,

    PRIMARY KEY (match_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS partition_leases (
    partition_id INT PRIMARY KEY,

    owned_by TEXT,
    lease_until TIMESTAMPTZ,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    idempotency_key TEXT NOT NULL,

    request_hash TEXT NOT NULL,
    response_status INT NOT NULL,
    response_body JSONB NOT NULL,

    ticket_id UUID REFERENCES tickets(ticket_id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS callback_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    match_id UUID NOT NULL REFERENCES matches(match_id),

    event_type TEXT NOT NULL DEFAULT 'match.created',
    callback_url TEXT NOT NULL,
    payload JSONB NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('pending', 'in_progress', 'delivered', 'failed')
    ) DEFAULT 'pending',

    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    locked_by TEXT,
    locked_until TIMESTAMPTZ,

    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_callback_events_pending
ON callback_events (status, next_attempt_at);
