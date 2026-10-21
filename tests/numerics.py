"""Refits warm-started from different fits agree to rounding, not bit for bit."""

import pytest


def approx_items(items: list[dict]) -> list[dict]:
    return [
        {
            **item,
            "mu": pytest.approx(item["mu"], rel=1e-9, abs=1e-12),
            "sigma_sq": pytest.approx(item["sigma_sq"], rel=1e-9),
        }
        for item in items
    ]
