from dataclasses import dataclass


@dataclass
class CallbackDispatcherMetrics:
    callbacks_claimed: int = 0
    callbacks_delivered: int = 0
    callbacks_failed: int = 0
    callback_retries: int = 0
    delivery_latency_ms: float = 0.0
    loop_duration_ms: float = 0.0
    loops_completed: int = 0

    def record_claimed(self, count: int) -> None:
        self.callbacks_claimed += count

    def record_delivered(self, latency_ms: float) -> None:
        self.callbacks_delivered += 1
        self.delivery_latency_ms = latency_ms

    def record_failed(self) -> None:
        self.callbacks_failed += 1

    def record_retry(self) -> None:
        self.callback_retries += 1

    def record_loop(self, duration_ms: float) -> None:
        self.loop_duration_ms = duration_ms
        self.loops_completed += 1

    def format_prometheus(self, dispatcher_id: str) -> str:
        labels = f'dispatcher_id="{dispatcher_id}"'
        lines = [
            f"pms_callback_dispatcher_info{{{labels}}} 1",
            f"pms_callback_events_claimed_total{{{labels}}} {self.callbacks_claimed}",
            f"pms_callback_delivered_total{{{labels}}} {self.callbacks_delivered}",
            f"pms_callback_failed_total{{{labels}}} {self.callbacks_failed}",
            f"pms_callback_retries_total{{{labels}}} {self.callback_retries}",
            f"pms_callback_delivery_latency_ms{{{labels}}} {self.delivery_latency_ms:.2f}",
            f"pms_callback_loop_duration_ms{{{labels}}} {self.loop_duration_ms:.2f}",
            f"pms_callback_loops_completed_total{{{labels}}} {self.loops_completed}",
        ]
        return "\n".join(lines) + "\n"
