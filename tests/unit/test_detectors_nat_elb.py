from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import clients, launch_instance
from tripwire.common.config import Config
from tripwire.sweep.detectors import elb_no_targets, nat_idle

REGION = "us-east-1"
CFG = Config()  # nat_idle_days=7
OTHER_TAG = {"Key": "env", "Value": "test"}


def make_subnets(ec2) -> tuple[str, list[str]]:
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"{REGION}{az}")[
            "Subnet"
        ]["SubnetId"]
        for i, az in enumerate("ab")
    ]
    return vpc, subnets


# --- nat_idle ---


def make_nat(ec2, tags: list[dict] | None = None) -> tuple[str, datetime]:
    _, [subnet, _] = make_subnets(ec2)
    alloc = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=subnet, AllocationId=alloc, TagSpecifications=tags or [])
    return nat["NatGateway"]["NatGatewayId"], nat["NatGateway"]["CreateTime"]


def put_bytes(cloudwatch, nat_id: str, when: datetime, value: float) -> None:
    cloudwatch.put_metric_data(
        Namespace="AWS/NATGateway",
        MetricData=[
            {
                "MetricName": "BytesOutToDestination",
                "Dimensions": [{"Name": "NatGatewayId", "Value": nat_id}],
                "Timestamp": when,
                "Value": value,
                "Unit": "Bytes",
            }
        ],
    )


def test_nat_finds_idle_gateway(aws):
    c = clients()
    nat_id, created = make_nat(c.ec2)
    now = created + timedelta(days=10)
    put_bytes(c.cloudwatch, nat_id, now - timedelta(days=1), 400_000)
    put_bytes(c.cloudwatch, nat_id, now - timedelta(days=2), 400_000)  # 0.8 MB total

    [f] = nat_idle.detect(c, REGION, CFG, now)

    assert f.resource_id == nat_id
    assert f.detector == "nat_idle"
    assert f.monthly_cost_usd == 32.85
    assert f.age_days == 10
    assert "data processing is extra" in f.reason
    assert f.fix_command == (
        f"aws ec2 delete-nat-gateway --nat-gateway-id {nat_id} --region {REGION}"
    )


def test_nat_no_datapoints_counts_as_idle(aws):
    c = clients()
    nat_id, created = make_nat(c.ec2)

    [f] = nat_idle.detect(c, REGION, CFG, created + timedelta(days=8))

    assert f.resource_id == nat_id


def test_nat_ignores_busy_gateway(aws):
    c = clients()
    nat_id, created = make_nat(c.ec2)
    now = created + timedelta(days=10)
    put_bytes(c.cloudwatch, nat_id, now - timedelta(days=3), 600_000)
    put_bytes(c.cloudwatch, nat_id, now - timedelta(days=1), 600_000)  # 1.2 MB total

    assert nat_idle.detect(c, REGION, CFG, now) == []


def test_nat_ignores_traffic_outside_window(aws):
    c = clients()
    nat_id, created = make_nat(c.ec2)
    now = created + timedelta(days=30)
    put_bytes(c.cloudwatch, nat_id, now - timedelta(days=20), 50_000_000)

    assert len(nat_idle.detect(c, REGION, CFG, now)) == 1


def test_nat_skips_gateway_younger_than_window(aws):
    c = clients()
    _, created = make_nat(c.ec2)

    assert nat_idle.detect(c, REGION, CFG, created + timedelta(days=6, hours=23)) == []
    assert len(nat_idle.detect(c, REGION, CFG, created + timedelta(days=7))) == 1


def test_nat_respects_ignore_tag(aws):
    c = clients()
    tags = [{"ResourceType": "natgateway", "Tags": [{"Key": "tripwire", "Value": "ignore"}]}]
    _, created = make_nat(c.ec2, tags=tags)

    assert nat_idle.detect(c, REGION, CFG, created + timedelta(days=10)) == []


# --- elb_no_targets ---


def make_lb(c, name: str = "alb", lb_type: str = "application", tags=None) -> tuple[str, str]:
    vpc, subnets = make_subnets(c.ec2)
    tags = tags or [OTHER_TAG]  # ELBv2 rejects an empty Tags list
    lb = c.elbv2.create_load_balancer(Name=name, Subnets=subnets, Type=lb_type, Tags=tags)
    lb = lb["LoadBalancers"][0]
    return vpc, lb["LoadBalancerArn"]


def add_target_group(c, vpc: str, lb_arn: str, protocol: str = "HTTP") -> str:
    tg = c.elbv2.create_target_group(Name="tg", Protocol=protocol, Port=80, VpcId=vpc)
    tg_arn = tg["TargetGroups"][0]["TargetGroupArn"]
    c.elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol=protocol,
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )
    return tg_arn


def now() -> datetime:
    return datetime.now(UTC)


def test_elb_finds_lb_without_target_groups(aws):
    c = clients()
    _, arn = make_lb(c)

    [f] = elb_no_targets.detect(c, REGION, CFG, now())

    assert f.resource_id == arn
    assert f.detector == "elb_no_targets"
    assert f.monthly_cost_usd == 16.43
    assert f.reason == "application load balancer alb has no target groups"
    assert f.fix_command == (
        f"aws elbv2 delete-load-balancer --load-balancer-arn {arn} --region {REGION}"
    )


def test_elb_finds_lb_with_empty_target_group(aws):
    c = clients()
    vpc, arn = make_lb(c, lb_type="network")
    add_target_group(c, vpc, arn, protocol="TCP")

    [f] = elb_no_targets.detect(c, REGION, CFG, now())

    assert f.resource_id == arn
    assert f.reason == "network load balancer alb has no registered targets"


def test_elb_ignores_lb_with_targets(aws):
    c = clients()
    vpc, arn = make_lb(c)
    tg_arn = add_target_group(c, vpc, arn)
    iid = launch_instance(c.ec2)
    c.elbv2.register_targets(TargetGroupArn=tg_arn, Targets=[{"Id": iid}])
    c.ec2.stop_instances(InstanceIds=[iid])  # unhealthy/unused still counts as registered

    assert elb_no_targets.detect(c, REGION, CFG, now()) == []


def test_elb_skips_gateway_lb(aws):
    c = clients()
    make_lb(c, lb_type="gateway")

    assert elb_no_targets.detect(c, REGION, CFG, now()) == []


@pytest.mark.parametrize("value", ["ignore", " IGNORE "])
def test_elb_respects_ignore_tag(aws, value):
    c = clients()
    make_lb(c, tags=[{"Key": "tripwire", "Value": value}])

    assert elb_no_targets.detect(c, REGION, CFG, now()) == []


def test_elb_batches_tag_lookups(aws, monkeypatch):
    c = clients()
    vpc, subnets = make_subnets(c.ec2)
    for i in range(21):
        tags = [{"Key": "tripwire", "Value": "ignore"}] if i == 20 else [OTHER_TAG]
        c.elbv2.create_load_balancer(Name=f"lb{i}", Subnets=subnets, Tags=tags)
    calls = []
    real = c.elbv2.describe_tags
    monkeypatch.setattr(
        c.elbv2, "describe_tags", lambda **kw: calls.append(len(kw["ResourceArns"])) or real(**kw)
    )

    findings = elb_no_targets.detect(c, REGION, CFG, now())

    assert calls == [20, 1]
    assert len(findings) == 20  # the 21st, in the second batch, is ignored


def test_elb_empty_region(aws):
    assert elb_no_targets.detect(clients("eu-west-3"), "eu-west-3", CFG, now()) == []
