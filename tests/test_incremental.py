from datetime import date

from china_equities_alpha.jobs.market import missing_range

R = (date(2023, 1, 1), date(2024, 12, 31))


def test_nothing_covered():
    assert missing_range(R, None) == [R]


def test_fully_covered():
    assert missing_range(R, (date(2022, 1, 1), date(2025, 1, 1))) == []


def test_extend_both_ends():
    assert missing_range(R, (date(2023, 6, 1), date(2024, 6, 30))) == [
        (date(2023, 1, 1), date(2023, 5, 31)), (date(2024, 7, 1), date(2024, 12, 31))]


def test_disjoint_request_stays_contiguous():
    # Covered 2020 only; asking for 2023-2024 must also fill 2021-2022 so there is no hole.
    assert missing_range(R, (date(2020, 1, 1), date(2020, 12, 31))) == [
        (date(2021, 1, 1), date(2024, 12, 31))]


def test_empty_request():
    assert missing_range((date(2024, 1, 2), date(2024, 1, 1)), None) == []
