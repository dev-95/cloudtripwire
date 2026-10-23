"""Shared helpers for detector tests. Use only inside the `aws` fixture."""

from functools import cache

import boto3

from tripwire.common.clients import RegionClients

FAKE_AMI = "ami-0123456789abcdef0"  # moto doesn't validate it when default AMIs aren't loaded


@cache
def _client(service: str, region: str):
    # Building an EC2 client takes ~0.25s, so reuse one per process. moto intercepts
    # calls from any client while a mock is active and resets its state between mocks.
    return boto3.client(service, region_name=region)


def clients(region: str = "us-east-1") -> RegionClients:
    return RegionClients(
        ec2=_client("ec2", region),
        elbv2=_client("elbv2", region),
        cloudwatch=_client("cloudwatch", region),
    )


def tag(key: str, value: str, resource: str) -> list[dict]:
    return [{"ResourceType": resource, "Tags": [{"Key": key, "Value": value}]}]


def launch_instance(ec2, **kwargs) -> str:
    resp = ec2.run_instances(ImageId=FAKE_AMI, MinCount=1, MaxCount=1, **kwargs)
    return resp["Instances"][0]["InstanceId"]
