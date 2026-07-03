CREATE TABLE IF NOT EXISTS tenants (
    tenant_id              TEXT        PRIMARY KEY,
    max_tickets_in_flight  INT         NOT NULL DEFAULT 1000,
    max_tickets_per_second INT         NOT NULL DEFAULT 100,
    callback_url           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id    TEXT        NOT NULL,
    tenant_id    TEXT        NOT NULL,
    region       TEXT        NOT NULL,
    queue_name   TEXT        NOT NULL,
    skill_rating FLOAT       NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'waiting',
    partition_id INT         NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS matches (
    match_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  TEXT        NOT NULL,
    region     TEXT        NOT NULL,
    queue_name TEXT        NOT NULL,
    status     TEXT        NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id  UUID REFERENCES matches(match_id),
    player_id TEXT NOT NULL,
    PRIMARY KEY (match_id, player_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key            TEXT        NOT NULL,
    tenant_id      TEXT        NOT NULL,
    request_hash   TEXT        NOT NULL,
    response_body  JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, tenant_id)
);

INSERT INTO tenants (tenant_id, max_tickets_in_flight, max_tickets_per_second, callback_url)
VALUES
  ('tenant-1', 1000, 100, 'http://localhost:9000/callback/tenant-1'),
  ('tenant-2', 500, 50, 'http://localhost:9000/callback/tenant-2')
ON CONFLICT DO NOTHING;
