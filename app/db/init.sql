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
