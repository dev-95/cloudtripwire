from moto import mock_aws

from tests.helpers import clients, launch_instance


def test_cached_clients_start_empty_in_each_mock(aws):
    # Two mocks one after the other, as the `aws` fixture does across two tests.
    # (Nested mocks share state in moto, so this ends the fixture's mock first.)
    # Fake credentials stay set by the fixture for both.
    c = clients()
    c.ec2.create_volume(AvailabilityZone="us-east-1a", Size=1)
    launch_instance(c.ec2)
    c.cloudwatch.put_metric_data(Namespace="T", MetricData=[{"MetricName": "m", "Value": 1}])
    vpc = c.ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    c.elbv2.create_target_group(Name="tg", Protocol="HTTP", Port=80, VpcId=vpc)
    assert c.ec2.describe_volumes()["Volumes"]
    aws.stop()

    with mock_aws():
        c2 = clients()
        assert c2.ec2 is c.ec2  # same cached client objects
        assert c2.ec2.describe_volumes()["Volumes"] == []
        assert c2.ec2.describe_instances()["Reservations"] == []
        assert c2.cloudwatch.list_metrics()["Metrics"] == []
        assert c2.elbv2.describe_target_groups()["TargetGroups"] == []

    aws.start()  # let the fixture's teardown stop it cleanly
