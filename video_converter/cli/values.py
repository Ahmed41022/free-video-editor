"""Reading option values: times, ranges, numbers within limits."""

from __future__ import annotations

import argparse

from ..core import parse_time
from ..timing import FADE_MAX, SPEED_MAX, SPEED_MIN
from ..video import ADJUSTMENTS


def _time(text: str) -> float:
    try:
        value = parse_time(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if value is None:
        raise argparse.ArgumentTypeError("empty time")
    return value


def _range(text: str) -> tuple[float, float]:
    start, sep, end = text.partition("-")
    if not sep:
        raise argparse.ArgumentTypeError(f"use START-END, e.g. 0:10-0:15 (got {text!r})")
    a, b = _time(start), _time(end)
    if b <= a:
        raise argparse.ArgumentTypeError(f"the end of {text!r} must be after its start")
    return a, b


def _speed(text: str) -> float:
    try:
        value = float(text.rstrip("xX×"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not SPEED_MIN <= value <= SPEED_MAX:
        raise argparse.ArgumentTypeError(f"speed must be between {SPEED_MIN:g} and {SPEED_MAX:g}")
    return value


def _seconds(text: str) -> float:
    value = _time(text)
    if value > FADE_MAX:
        raise argparse.ArgumentTypeError(f"at most {FADE_MAX:g} seconds")
    return value


def _between(name: str, low: float, high: float):
    """An argument type: a number from `low` to `high`, refused with a clear message otherwise."""

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low:g} and {high:g}")
        return value

    return parse


def _whole(name: str, low: int, high: int):
    """An argument type: a whole number from `low` to `high`."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low} and {high}")
        return value

    return parse


def _adjustment(name: str):
    low, high, _ = ADJUSTMENTS[name]
    return _between(name, low, high)
