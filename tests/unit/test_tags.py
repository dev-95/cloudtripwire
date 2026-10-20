from datetime import UTC, datetime, timedelta, timezone

import pytest

from tripwire.common.tags import (
    get_ttl_value,
    is_expired,
    is_ignored,
    needs_warning,
    parse_ttl,
    tags_to_dict,
)

CREATED = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_tags_to_dict():
    assert tags_to_dict([{"Key": "a", "Value": "1"}, {"Key": "b", "Value": "2"}]) == {
        "a": "1",
        "b": "2",
    }
    assert tags_to_dict(None) == {}
    assert tags_to_dict([]) == {}


@pytest.mark.parametrize(
    "tags",
    [{"tripwire": "ignore"}, {"TripWire": "IGNORE"}, {" tripwire ": " Ignore "}],
)
def test_is_ignored(tags):
    assert is_ignored(tags)


@pytest.mark.parametrize("tags", [{}, {"tripwire": "watch"}, {"other": "ignore"}])
def test_not_ignored(tags):
    assert not is_ignored(tags)


def test_get_ttl_value():
    assert get_ttl_value({"TTL": "4h"}) == "4h"
    assert get_ttl_value({"Name": "x"}) is None
    assert get_ttl_value({}) is None


@pytest.mark.parametrize(
    ("value", "delta"),
    [
        ("30m", timedelta(minutes=30)),
        ("4h", timedelta(hours=4)),
        ("2d", timedelta(days=2)),
        ("1w", timedelta(weeks=1)),
        (" 4H ", timedelta(hours=4)),
        ("2D", timedelta(days=2)),
    ],
)
def test_parse_relative(value, delta):
    assert parse_ttl(value, CREATED) == CREATED + delta


def test_parse_relative_converts_to_utc():
    created = datetime(2026, 10, 19, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert parse_ttl("1h", created) == datetime(2026, 10, 19, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-20", datetime(2026, 10, 20, tzinfo=UTC)),
        ("2026-10-20T18:00Z", datetime(2026, 10, 20, 18, 0, tzinfo=UTC)),
        ("2026-10-20t18:00z", datetime(2026, 10, 20, 18, 0, tzinfo=UTC)),
        (" 2026-10-20T18:00:30Z ", datetime(2026, 10, 20, 18, 0, 30, tzinfo=UTC)),
    ],
)
def test_parse_absolute(value, expected):
    assert parse_ttl(value, CREATED) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "0h",
        "-2d",
        "",
        "   ",
        None,
        "2026-13-45",
        "4",
        "h",
        "4y",
        "4 h",
        "2026-10-20T18:00",  # no Z: timezone ambiguous
        "2026-10-20T18:00+05:00",
        "20261020",
        "99999999999w",
    ],
)
def test_parse_invalid(value):
    assert parse_ttl(value, CREATED) is None


def test_naive_datetimes_rejected():
    naive = datetime(2026, 10, 19, 12, 0)
    with pytest.raises(ValueError):
        parse_ttl("4h", naive)
    with pytest.raises(ValueError):
        is_expired(naive, CREATED)
    with pytest.raises(ValueError):
        needs_warning(CREATED, naive)


def test_is_expired():
    assert not is_expired(CREATED, CREATED - timedelta(seconds=1))
    assert is_expired(CREATED, CREATED)
    assert is_expired(CREATED, CREATED + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("before_expiry", "expected"),
    [
        (timedelta(hours=1, seconds=1), False),  # just outside window
        (timedelta(hours=1), True),  # window start, inclusive
        (timedelta(minutes=30), True),
        (timedelta(seconds=1), True),
        (timedelta(0), False),  # expired: warning no longer applies
        (-timedelta(minutes=5), False),
    ],
)
def test_needs_warning(before_expiry, expected):
    assert needs_warning(CREATED, CREATED - before_expiry) is expected


def test_needs_warning_custom_window():
    assert needs_warning(CREATED, CREATED - timedelta(hours=2), window=timedelta(hours=3))
