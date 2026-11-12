"""Structured logging, run metrics, and Prometheus exposition (Section 7.1)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


@dataclass
class MetricsRegistry:
    """In-process Prometheus-compatible counters and stage duration histograms."""

    counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    duration_sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    duration_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = field(
        default_factory=lambda: defaultdict(int)
    )

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        self.counters[key] += value

    def observe_duration(self, name: str, seconds: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        self.duration_sums[key] += seconds
        self.duration_counts[key] += 1

    @contextmanager
    def stage_span(self, stage: str, project_id: str = "default") -> Iterator[None]:
        start = time.perf_counter()
        self.inc("l2c_stage_runs_total", stage=stage, project_id=project_id)
        try:
            yield
        except Exception:
            self.inc("l2c_stage_errors_total", stage=stage, project_id=project_id)
            raise
        finally:
            elapsed = time.perf_counter() - start
            self.observe_duration("l2c_stage_duration_seconds", elapsed, stage=stage)

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for (name, labels), val in sorted(self.counters.items()):
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{lbl}}} {val}" if lbl else f"{name} {val}")
        for (name, labels), val in sorted(self.duration_sums.items()):
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            cnt = self.duration_counts[(name, labels)]
            lines.append(f"{name}_sum{{{lbl}}} {val:.6f}" if lbl else f"{name}_sum {val:.6f}")
            lines.append(f"{name}_count{{{lbl}}} {cnt}" if lbl else f"{name}_count {cnt}")
        return "\n".join(lines) + ("\n" if lines else "")


metrics = MetricsRegistry()
