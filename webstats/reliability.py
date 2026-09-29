"""Detection helpers for explainable reliability incidents."""

from __future__ import annotations

from datetime import date
import math
from statistics import fmean, pstdev
from typing import Any


BASELINE_DAYS = 28
TAIL_DAYS = 7


def detect_error_bursts(
    series: list[dict[str, Any]], selected_start: date, selected_end: date,
) -> list[dict[str, Any]]:
    """Detect application-error bursts and follow them through recovery."""
    if not series:
        return []
    days = [date.fromisoformat(row["day"]) for row in series]
    errors = [int(row.get("app_errors", 0)) for row in series]
    server_errors = [int(row.get("server_errors", 0)) for row in series]
    triggers = []
    baselines: dict[int, tuple[float, int]] = {}
    for index, day in enumerate(days):
        if not selected_start <= day <= selected_end:
            continue
        history = errors[max(0, index - BASELINE_DAYS):index]
        baseline = fmean(history) if history else 0.0
        deviation = pstdev(history) if len(history) > 1 else 0.0
        threshold = max(
            3, math.ceil(baseline * 2), math.ceil(baseline + 2 * deviation),
        )
        baselines[index] = (baseline, threshold)
        if errors[index] >= threshold and errors[index] >= baseline + 2:
            triggers.append(index)
    if not triggers:
        return []

    groups: list[list[int]] = [[triggers[0]]]
    for index in triggers[1:]:
        if index - groups[-1][-1] <= 2:
            groups[-1].append(index)
        else:
            groups.append([index])

    incidents = []
    for group in groups:
        first = group[0]
        last = group[-1]
        baseline, threshold = baselines[first]
        recovery_line = max(1.0, baseline * 1.25)
        end = last
        cursor = last + 1
        while (
            cursor < len(errors) and cursor <= last + TAIL_DAYS
            and errors[cursor] > recovery_line
        ):
            end = cursor
            cursor += 1
        peak = max(range(first, end + 1), key=lambda item: errors[item])
        recovered = cursor < len(errors) and errors[cursor] <= recovery_line
        incidents.append({
            "kind": "error-burst",
            "start": days[first].isoformat(),
            "end": days[end].isoformat(),
            "peak_day": days[peak].isoformat(),
            "peak_errors": errors[peak],
            "errors": sum(errors[first:end + 1]),
            "server_errors": sum(server_errors[first:end + 1]),
            "baseline_average": round(baseline, 1),
            "threshold": threshold,
            "recovered": recovered,
            "recovery_day": days[cursor].isoformat() if recovered else None,
            "duration_days": end - first + 1,
        })
    return incidents
