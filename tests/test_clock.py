"""Where time is decided, and what a span has to be for the instant it names to exist."""

from datetime import datetime, timedelta, timezone

import pytest

from cachefy.clock import WIDEST_INSTANT, as_utc, naive_utc, now, real, spanned, waited
from cachefy.errors import CacheError


def test_every_instant_this_library_writes_down_is_utc():
    assert now().tzinfo is timezone.utc


def test_a_naive_instant_is_read_as_the_utc_one_it_says():
    naive = datetime(2026, 8, 1, 4, 0, 0)

    assert as_utc(naive) == datetime(2026, 8, 1, 4, 0, 0, tzinfo=timezone.utc)
    assert as_utc(None) is None


def test_an_instant_in_another_zone_is_read_as_the_utc_one_it_names():
    elsewhere = datetime(2026, 8, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=-3)))

    assert as_utc(elsewhere) == datetime(2026, 8, 1, 4, 0, 0, tzinfo=timezone.utc)


def test_an_instant_reaching_a_column_with_no_offset_carries_none():
    assert naive_utc(datetime(2026, 8, 1, 4, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 8, 1, 4, 0, 0)
    assert naive_utc(None) is None


@pytest.mark.parametrize("value", [0, 1, -1, 1.5, 3600.0])
def test_a_real_number_of_seconds_is_taken(value):
    assert real(value, "the span") is None


@pytest.mark.parametrize("value", [True, False, "60", None, timedelta(seconds=60)])
def test_what_a_store_would_not_read_back_as_that_number_is_refused(value):
    with pytest.raises(CacheError, match="real number of seconds"):
        real(value, "the span")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_that_passes_every_comparison_is_refused_before_the_arithmetic(value):
    with pytest.raises(CacheError, match="what it has to be is a real number"):
        real(value, "the span")


def test_a_span_is_a_timedelta_and_never_a_number_of_seconds():
    assert spanned(timedelta(minutes=1), "the span") == 60.0

    with pytest.raises(CacheError, match="a span is a timedelta"):
        spanned(60, "the span")


def test_a_span_added_to_now_is_bounded_by_what_is_left_of_the_range():
    assert waited(3600, "the span") is None

    with pytest.raises(CacheError, match="left between now and the last instant"):
        waited((WIDEST_INSTANT - now()).total_seconds() + 86400, "the span")


def test_a_span_reaching_exactly_the_last_instant_is_taken():
    """The bound is the seconds that are left, so refusing the very last of them is a lifetime nobody may ask for."""
    left = (WIDEST_INSTANT - now()) // timedelta(seconds=1)

    assert waited(left, "the span") is None

    with pytest.raises(CacheError, match="left between now and the last instant"):
        waited(left + 1, "the span")
