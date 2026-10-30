"""Idle opportunities, measured from completion, never interrupts an active run."""

import math


def interval(value, *, minimum=120, maximum=600):
    value = min(100, max(0, float(value)))
    return None if value <= 5 else minimum + (maximum - minimum) * (1 - value / 100) ** 2


class IdleClock:
    def __init__(self, now, *, adaptive=False, fixed=0, minimum=120, maximum=600):
        if adaptive and fixed:
            raise ValueError("Adaptive and fixed idle wake are mutually exclusive")
        if not (math.isfinite(minimum) and math.isfinite(maximum) and 0 < minimum <= maximum):
            raise ValueError("Idle wake requires finite 0 < minimum <= maximum")
        self.base, self.adaptive, self.fixed = now, adaptive, fixed
        self.minimum, self.maximum = minimum, maximum

    def completed(self, now):
        self.base = now

    def state(self, now, value, *, busy=False, waiting=False):
        seconds = (
            interval(value, minimum=self.minimum, maximum=self.maximum)
            if self.adaptive
            else (self.fixed or None)
        )
        due = self.base + seconds if seconds is not None and not busy and not waiting else None
        return {
            "adaptive": self.adaptive,
            "sleeping": value <= 5,
            "interval_seconds": seconds,
            "remaining_seconds": max(0, due - now) if due is not None else None,
            "ready": due is not None and now >= due,
        }
