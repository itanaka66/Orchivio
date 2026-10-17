from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orchivio.model.cron import CronError, parse_cron


def t(*a):
    return datetime(*a, tzinfo=UTC)


@pytest.mark.parametrize("text", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *",
                                  "* * * 13 *", "* * * * 8", "a * * * *", "*/0 * * * *", "5-2 * * * *", "1- * * * *",
                                  "*/x * * * *", ", * * * *", "0 0 31 2 *", "-1 * * * *", "? * * * *", "0 0 * * MON"])
def test_rejects_invalid_expressions(text):
    with pytest.raises(CronError):
        parse_cron(text)


def test_next_after_basics():
    assert parse_cron("* * * * *").next_after(t(2026, 1, 1, 0, 0)) == t(2026, 1, 1, 0, 1)
    assert parse_cron("*/15 * * * *").next_after(t(2026, 1, 1, 10, 15)) == t(2026, 1, 1, 10, 30)
    assert parse_cron("30 2 * * *").next_after(t(2026, 1, 1, 2, 30)) == t(2026, 1, 2, 2, 30)
    assert parse_cron("0 9 * * 1").next_after(t(2026, 1, 1)) == t(2026, 1, 5, 9, 0)  # Monday
    assert parse_cron("0 0 1 1 *").next_after(t(2026, 1, 1)) == t(2027, 1, 1)
    assert parse_cron("0,30 8-9 * * *").next_after(t(2026, 1, 1, 8, 30)) == t(2026, 1, 1, 9, 0)
    assert parse_cron("5/20 * * * *").next_after(t(2026, 1, 1, 0, 0)) == t(2026, 1, 1, 0, 5)
    assert parse_cron("5/20 * * * *").next_after(t(2026, 1, 1, 0, 5)) == t(2026, 1, 1, 0, 25)


def test_seconds_are_ignored_and_naive_is_utc():
    assert parse_cron("* * * * *").next_after(t(2026, 1, 1, 0, 0, 59)) == t(2026, 1, 1, 0, 1)
    assert parse_cron("0 12 * * *").next_after(datetime(2026, 1, 1, 0, 0)) == t(2026, 1, 1, 12, 0)


def test_sunday_is_zero_or_seven():
    sunday = t(2026, 1, 4)
    assert sunday.isoweekday() == 7
    assert parse_cron("0 0 * * 0").next_after(t(2026, 1, 1)) == sunday
    assert parse_cron("0 0 * * 7").next_after(t(2026, 1, 1)) == sunday
    assert parse_cron("0 0 * * 5-7").matches(sunday)


def test_day_of_month_and_weekday_are_ored_when_both_restricted():
    c = parse_cron("0 0 13 * 5")  # the 13th, or any Friday
    assert c.matches(t(2026, 2, 13))  # a Friday and the 13th
    assert c.matches(t(2026, 1, 13))  # a Tuesday, the 13th
    assert c.matches(t(2026, 1, 2))  # a Friday
    assert not c.matches(t(2026, 1, 14))


def test_leap_day_and_never():
    assert parse_cron("0 0 29 2 *").next_after(t(2026, 1, 1)) == t(2028, 2, 29)
    with pytest.raises(CronError):
        parse_cron("0 0 30 2 *")


def test_between_is_half_open_and_limited():
    c = parse_cron("*/10 * * * *")
    got = c.between(t(2026, 1, 1, 0, 0), t(2026, 1, 1, 0, 30))
    assert got == [t(2026, 1, 1, 0, m) for m in (10, 20, 30)]
    assert len(c.between(t(2026, 1, 1), t(2026, 6, 1), limit=5)) == 5
    assert c.between(t(2026, 1, 1, 0, 30), t(2026, 1, 1, 0, 30)) == []


@given(st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2040, 1, 1)),
       st.sampled_from(["* * * * *", "*/7 * * * *", "0 */5 * * *", "15 3 * * 1-5", "0 0 1 * *", "59 23 31 12 *", "0 0 * * 0"]))
def test_next_after_is_the_first_match(start, text):
    c = parse_cron(text)
    start = start.replace(tzinfo=UTC)
    nxt = c.next_after(start)
    assert nxt > start and c.matches(nxt)
    probe = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if (nxt - probe) < timedelta(days=2):  # cheap brute-force cross-check for nearby matches
        while probe < nxt:
            assert not c.matches(probe)
            probe += timedelta(minutes=1)
