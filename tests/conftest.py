"""Shared pytest fixtures (moto, fake env vars)."""

import pytest
from moto import mock_aws

from tripwire.common import regions


@pytest.fixture
def aws(monkeypatch):
    """Fake credentials + moto for the whole test. Never touches real AWS."""
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "MOTO_EC2_LOAD_DEFAULT_AMIS": "false",  # loading the AMI catalog costs ~1s per test
    }.items():
        monkeypatch.setenv(k, v)
    regions.get_client.cache_clear()
    with mock_aws():
        yield
    regions.get_client.cache_clear()
