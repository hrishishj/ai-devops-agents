"""THROWAWAY FILE for trying the AI review agents. This one is correct on purpose. DO NOT MERGE."""


def clamp(value: float, low: float, high: float) -> float:
    """Limit `value` to the range [low, high]."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(high, value))


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("mean() of an empty list")
    return sum(values) / len(values)
