"""Web control plane removed — route/job API tests skipped."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="web UI removed; use physics-router route (native progress window)"
)


def test_web_ui_removed():
    assert False, "unreachable"
