from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "pms"
    app_env: str = "local"

    api_host: str = "0.0.0.0"
    api_port: int = 8080

    database_url: str = "postgresql://pms:pms@postgres:5432/pms"
    redis_url: str = "redis://redis:6379/0"

    matchmaking_partitions: int = 128

    default_ticket_rate_limit_per_minute: int = 300
    default_max_waiting_tickets: int = 5000
    default_max_partition_depth: int = 1000

    max_partition_depth: int = 10000

    load_shedding_enabled: bool = True
    db_latency_shed_threshold_ms: int = 200

    worker_id: str = "worker-local-1"
    worker_metrics_host: str = "0.0.0.0"
    worker_metrics_port: int = 9090
    worker_lease_seconds: int = 15
    worker_loop_interval_ms: int = 500
    worker_loop_jitter_pct: float = 0.25
    worker_lease_renew_jitter_pct: float = 0.1
    worker_partition_batch_size: int = 8
    worker_ticket_batch_size: int = 100
    worker_loop_budget_ms: int = 2000
    worker_max_pairs_per_loop: int = 20
    worker_freshness_bias: bool = True
    worker_elastic_rebalance_enabled: bool = True
    worker_heartbeat_seconds: int = 30

    match_size: int = 2
    skill_delta_initial: int = 100
    skill_delta_after_30s: int = 200
    skill_delta_after_60s: int = 400

    callback_dispatcher_id: str = "callback-local-1"
    callback_metrics_host: str = "0.0.0.0"
    callback_metrics_port: int = 9091
    callback_batch_size: int = 50
    callback_tenant_concurrency_limit: int = 10
    callback_timeout_seconds: int = 3
    callback_max_attempts: int = 5
    callback_base_backoff_seconds: int = 2
    callback_max_backoff_seconds: int = 60
    callback_jitter_seconds: int = 3

    metrics_enabled: bool = True
    log_level: str = "error"


@lru_cache
def get_settings() -> Settings:
    return Settings()
