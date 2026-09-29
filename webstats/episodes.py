"""Explainable traffic-episode detection over continuous daily totals."""

from __future__ import annotations

from datetime import date
import math
from statistics import fmean, pstdev
from typing import Any, Iterable


BASELINE_DAYS = 28
AFTERMATH_DAYS = 7


def _mean(values: Iterable[int]) -> float:
    values = list(values)
    return fmean(values) if values else 0.0


def detect_episodes(
    series: list[dict[str, Any]], selected_start: date, selected_end: date,
) -> list[dict[str, Any]]:
    """Return burst episodes whose trigger days fall in the selected window."""
    if not series:
        return []
    days = [date.fromisoformat(row["day"]) for row in series]
    values = [int(row["requests"]) for row in series]
    selected_indices = [
        index for index, day in enumerate(days)
        if selected_start <= day <= selected_end
    ]
    triggers = []
    thresholds: dict[int, dict[str, float]] = {}
    for index in selected_indices:
        history = values[max(0, index - BASELINE_DAYS):index]
        baseline = _mean(history)
        deviation = pstdev(history) if len(history) > 1 else 0.0
        threshold = max(5, math.ceil(baseline * 2), math.ceil(baseline + 2 * deviation))
        thresholds[index] = {
            "baseline": baseline,
            "deviation": deviation,
            "threshold": float(threshold),
        }
        if values[index] >= threshold and values[index] >= baseline + 3:
            triggers.append(index)
    if not triggers:
        return []

    groups: list[list[int]] = [[triggers[0]]]
    for index in triggers[1:]:
        if index - groups[-1][-1] <= 2:
            groups[-1].append(index)
        else:
            groups.append([index])

    episodes = []
    available_last = len(values) - 1
    for group in groups:
        first_trigger = group[0]
        last_trigger = group[-1]
        baseline = thresholds[first_trigger]["baseline"]
        recovery_line = max(1.0, baseline * 1.25)
        end_index = last_trigger
        cursor = last_trigger + 1
        while (
            cursor <= available_last and cursor <= last_trigger + AFTERMATH_DAYS
            and values[cursor] > recovery_line
        ):
            end_index = cursor
            cursor += 1
        peak_index = max(
            range(first_trigger, end_index + 1), key=lambda item: values[item]
        )
        duration = end_index - first_trigger + 1
        total = sum(values[first_trigger:end_index + 1])
        expected = round(baseline * duration)
        excess = max(0, total - expected)
        aftermath = values[
            end_index + 1:min(len(values), end_index + 1 + AFTERMATH_DAYS)
        ]
        aftermath_average = round(_mean(aftermath), 1) if aftermath else None
        enough_aftermath = len(aftermath) >= 3
        if not enough_aftermath:
            lasting_effect = "unresolved"
        elif aftermath_average >= baseline * 1.5 and aftermath_average >= baseline + 2:
            lasting_effect = "sustained"
        else:
            lasting_effect = "returned"
        end_value = values[end_index]
        if end_index == available_last and end_value > recovery_line:
            trajectory = "active"
        elif cursor <= available_last and values[cursor] <= recovery_line:
            trajectory = "recovered"
        elif end_value < values[peak_index]:
            trajectory = "fading"
        else:
            trajectory = "plateau"
        chart_start = max(0, first_trigger - 3)
        chart_end = min(len(values) - 1, end_index + AFTERMATH_DAYS)
        timeline = []
        for index in range(chart_start, chart_end + 1):
            if first_trigger <= index <= end_index:
                phase = "episode"
            elif index < first_trigger:
                phase = "before"
            else:
                phase = "after"
            timeline.append({
                "day": days[index].isoformat(),
                "requests": values[index],
                "phase": phase,
                "peak": index == peak_index,
            })
        episodes.append({
            "start": days[first_trigger].isoformat(),
            "end": days[end_index].isoformat(),
            "peak_day": days[peak_index].isoformat(),
            "peak_requests": values[peak_index],
            "duration_days": duration,
            "requests": total,
            "expected_requests": expected,
            "excess_requests": excess,
            "baseline_average": round(baseline, 1),
            "trigger_threshold": int(thresholds[first_trigger]["threshold"]),
            "peak_multiple": (
                round(values[peak_index] / baseline, 1) if baseline else None
            ),
            "trajectory": trajectory,
            "lasting_effect": lasting_effect,
            "aftermath_days": len(aftermath),
            "aftermath_average": aftermath_average,
            "timeline": timeline,
        })
    return episodes
