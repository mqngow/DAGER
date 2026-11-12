"""Decision engine (multi-horizon 1m, 3m, 6m, 1y, 3y, 5y investment evaluation)."""

from dager.core.evaluator.ranker import (
    classify_verdict,
    compute_horizon_signals,
    evaluate_company,
)
from dager.core.evaluator.valuation import (
    compute_valuation_breakdown,
    estimate_intrinsic_fair_value,
)

__all__ = [
    "estimate_intrinsic_fair_value",
    "compute_valuation_breakdown",
    "classify_verdict",
    "compute_horizon_signals",
    "evaluate_company",
]


