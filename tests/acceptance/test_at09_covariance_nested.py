"""AT-09 — measurement covariance accounted for across nested readers (HB §21).

Closes: I3. The posterior variance of a load seen by three nested readers is at
least 0.9× its single-reader variance (no false independence gain), and the load
is counted once.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I3"),
]


def test_nested_readers_do_not_gain_false_independence() -> None:
    """Nested readers share information; variance is not naively reduced."""
    from wattmosaic_engine.inference import infer_jointly  # target surface (I3)

    single = infer_jointly(reader_deltas={"ct_a": 45.0}, reader_sigmas={"ct_a": 2.0})
    nested = infer_jointly(
        reader_deltas={"ct_a": 45.0, "ct_sub": 45.0, "ct_main": 45.0},
        reader_sigmas={"ct_a": 2.0, "ct_sub": 2.0, "ct_main": 2.0},
    )
    assert single.cov is not None and nested.cov is not None
    var_single = float(np.asarray(single.cov)[0, 0])
    var_nested = float(np.asarray(nested.cov)[0, 0])
    assert var_nested >= 0.9 * var_single

    total = sum(e.point_w or 0.0 for e in nested.device_estimates)
    assert total == pytest.approx(45.0, abs=2.0)
