"""Shared helpers for moto tests. Use only inside the `aws` fixture."""

from functools import cache

import boto3

from tripwire.common.clients import RegionClients

FAKE_AMI = "ami-0123456789abcdef0"  # moto doesn't validate it when default AMIs aren't loaded


@cache
def client(service: str, region: str = "us-east-1"):
    # Building an EC2 client takes ~0.25s, so reuse one per process. moto intercepts
    # calls from any client while a mock is active and resets its state between mocks.
    return boto3.client(service, region_name=region)


@cache
def dynamodb(region: str = "us-east-1"):
    # Same reason: building a boto3 resource costs ~0.4s.
    return boto3.resource("dynamodb", region_name=region)


def clients(region: str = "us-east-1") -> RegionClients:
    return RegionClients(
        ec2=client("ec2", region),
        elbv2=client("elbv2", region),
        cloudwatch=client("cloudwatch", region),
    )


def tag(key: str, value: str, resource: str) -> list[dict]:
    return [{"ResourceType": resource, "Tags": [{"Key": key, "Value": value}]}]


def launch_instance(ec2, **kwargs) -> str:
    resp = ec2.run_instances(ImageId=FAKE_AMI, MinCount=1, MaxCount=1, **kwargs)
    return resp["Instances"][0]["InstanceId"]
