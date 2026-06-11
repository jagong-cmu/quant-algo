"""Tiny bootstrap / permutation helpers (no numpy/scipy).

Used to put uncertainty around the adaptive-minus-static return difference so the
harness never reports a single aggregated number without its error bars. Seeded
for reproducibility (Math.random-free determinism is not required here -- this is
ordinary application code, not a workflow script).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional


@dataclass
class DiffTest:
    n: int
    mean: float
    ci_lo: float
    ci_hi: float
    p_value: float           # permutation test of mean == 0
    significant: bool        # CI excludes 0 AND p < 0.05


def _percentile(sorted_xs: list[float], p: float) -> float:
    if not sorted_xs:
        return float("nan")
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    k = (len(sorted_xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def bootstrap_permutation(diffs: list[float], n_boot: int = 10000,
                          seed: int = 20260611) -> Optional[DiffTest]:
    """Bootstrap 95% CI for the mean of paired diffs + sign-flip permutation
    p-value (null: distribution symmetric around 0, i.e. no effect)."""
    diffs = [d for d in diffs if d is not None]
    n = len(diffs)
    if n < 2:
        return None
    rng = random.Random(seed)
    obs_mean = sum(diffs) / n

    boot_means = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    ci_lo = _percentile(boot_means, 2.5)
    ci_hi = _percentile(boot_means, 97.5)

    # sign-flip permutation test
    count = 0
    abs_obs = abs(obs_mean)
    for _ in range(n_boot):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs) / n
        if abs(s) >= abs_obs:
            count += 1
    p = (count + 1) / (n_boot + 1)

    significant = (ci_lo > 0 or ci_hi < 0) and p < 0.05
    return DiffTest(n=n, mean=obs_mean, ci_lo=ci_lo, ci_hi=ci_hi, p_value=p, significant=significant)
