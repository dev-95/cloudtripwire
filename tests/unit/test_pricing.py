import logging

import pytest

from tripwire.common import pricing


def test_ebs_monthly():
    assert pricing.ebs_monthly(20, "gp3") == 1.60
    assert pricing.ebs_monthly(100, "gp2") == 10.00
    assert pricing.ebs_monthly(1, "sc1") == 0.02  # 0.015 rounded


def test_ebs_unknown_type_falls_back_to_gp2(caplog):
    with caplog.at_level(logging.WARNING):
        assert pricing.ebs_monthly(10, "gp9") == 1.00
    assert "unknown_volume_type" in caplog.text


def test_fixed_monthly_prices():
    assert pricing.snapshot_monthly(8) == 0.40
    assert pricing.public_ipv4_monthly() == 3.65
    assert pricing.nat_gateway_monthly() == 32.85
    assert pricing.load_balancer_monthly("application") == 16.43
    assert pricing.load_balancer_monthly("network") == 16.43


def test_unknown_lb_type_raises():
    with pytest.raises(KeyError):
        pricing.load_balancer_monthly("mystery")
