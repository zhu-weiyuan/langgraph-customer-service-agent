"""Thread-safe, per-provider/model circuit breaker for upstream LLM calls."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised before an upstream call when its circuit is open."""


@dataclass
class _Circuit:
    failures: int = 0
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0
    half_open_in_flight: bool = False


class CircuitBreaker:
    """Failure isolation keyed by provider/model rather than process-wide state.

    The breaker uses a lock, so its closed → open → half-open → closed/re-open
    transitions remain safe while the threaded HTTP server drains in-flight
    requests during SIGTERM handling.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout must be >= 0")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._circuits: Dict[str, _Circuit] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}:{model}"

    def allow_request(self, provider: str, model: str) -> None:
        key = self.key(provider, model)
        with self._lock:
            circuit = self._circuits.setdefault(key, _Circuit())
            if circuit.state == CircuitState.OPEN:
                if self._clock() - circuit.opened_at < self.recovery_timeout:
                    raise CircuitOpenError(f"Circuit open for {key}")
                circuit.state = CircuitState.HALF_OPEN
            if circuit.state == CircuitState.HALF_OPEN:
                if circuit.half_open_in_flight:
                    raise CircuitOpenError(f"Circuit half-open; probe already in flight for {key}")
                circuit.half_open_in_flight = True

    def record_success(self, provider: str, model: str) -> None:
        with self._lock:
            circuit = self._circuits.setdefault(self.key(provider, model), _Circuit())
            circuit.failures = 0
            circuit.state = CircuitState.CLOSED
            circuit.opened_at = 0.0
            circuit.half_open_in_flight = False

    def record_failure(self, provider: str, model: str) -> None:
        with self._lock:
            circuit = self._circuits.setdefault(self.key(provider, model), _Circuit())
            circuit.half_open_in_flight = False
            circuit.failures += 1
            # A failed recovery probe re-opens immediately.
            if circuit.state == CircuitState.HALF_OPEN or circuit.failures >= self.failure_threshold:
                circuit.state = CircuitState.OPEN
                circuit.opened_at = self._clock()

    def state(self, provider: str, model: str) -> CircuitState:
        with self._lock:
            return self._circuits.get(self.key(provider, model), _Circuit()).state
