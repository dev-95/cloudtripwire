"""Fallback price table + monthly cost helpers.

All prices are us-east-1 on-demand list prices (USD) and are estimates for every
other region. Verified 2026-10-19 against the AWS public Price List files below;
the human-readable pages only show worked examples with the same numbers.
"""

import json
import logging
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730

# EBS storage per GB-month (storage only; provisioned IOPS/throughput not included).
# Source: https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/ebs.json
# Page: https://aws.amazon.com/ebs/pricing/ (verified 2026-10-19)
EBS_GB_MONTH = {
    "gp3": 0.08,
    "gp2": 0.10,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,  # magnetic, previous generation
}

# EBS snapshot, standard tier, per GB-month. Same source as EBS (verified 2026-10-19).
SNAPSHOT_GB_MONTH = 0.05

# Public IPv4 address, in-use or idle, per hour.
# Source: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonVPC/current/us-east-1/index.json
# Page: https://aws.amazon.com/vpc/pricing/ (verified 2026-10-19)
PUBLIC_IPV4_HOUR = 0.005

# NAT gateway per hour (data processing $0.045/GB not included).
# Source: https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/natgateway.json
# Page: https://aws.amazon.com/vpc/pricing/ (verified 2026-10-19)
NAT_GATEWAY_HOUR = 0.045

# Load balancer per hour (LCU / data charges not included). Gateway LB is per AZ.
# Source: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSELB/current/us-east-1/index.json
# Page: https://aws.amazon.com/elasticloadbalancing/pricing/ (verified 2026-10-19)
LOAD_BALANCER_HOUR = {
    "application": 0.0225,
    "network": 0.0225,
    "gateway": 0.0125,
    "classic": 0.025,
}


def _usd(quantity: float, unit_price: float) -> float:
    # Multiply in Decimal and round half-up: float math sends 1 GB x 0.015 to 0.01.
    amount = Decimal(str(quantity)) * Decimal(str(unit_price))
    return float(amount.quantize(Decimal("0.01"), ROUND_HALF_UP))


def ebs_monthly(size_gb: float, volume_type: str) -> float:
    price = EBS_GB_MONTH.get(volume_type)
    if price is None:
        logger.warning(json.dumps({"event": "unknown_volume_type", "volume_type": volume_type}))
        price = EBS_GB_MONTH["gp2"]
    return _usd(size_gb, price)


def snapshot_monthly(size_gb: float) -> float:
    return _usd(size_gb, SNAPSHOT_GB_MONTH)


def public_ipv4_monthly() -> float:
    return _usd(PUBLIC_IPV4_HOUR, HOURS_PER_MONTH)


def nat_gateway_monthly() -> float:
    return _usd(NAT_GATEWAY_HOUR, HOURS_PER_MONTH)


def load_balancer_monthly(lb_type: str) -> float:
    """lb_type is the ELBv2 `Type` field (application/network/gateway) or 'classic'."""
    return _usd(LOAD_BALANCER_HOUR[lb_type], HOURS_PER_MONTH)
