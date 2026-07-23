"""Web control plane removed — module kept only for historical skip markers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="web UI removed; use physics-router route (native progress window)"
)


def test_web_ui_removed():
    assert False, "unreachable"
