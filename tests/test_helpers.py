from datetime import datetime, timezone

from app.main import compute_late_minutes, compute_overtime, compute_work_hours

UTC = timezone.utc


def test_late_grace_period_is_strictly_more_than_ten_minutes():
    assert compute_late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC), "09:30", "18:30", "2026-07-06") == 0
    assert compute_late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC), "09:30", "18:30", "2026-07-06") == 10


def test_late_minutes_are_floored_from_shift_start():
    assert compute_late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC), "09:30", "18:30", "2026-07-06") == 45


def test_overtime_requires_at_least_thirty_minutes():
    assert compute_overtime(datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC), "18:30", "2026-07-06", "09:30") == 0
    assert compute_overtime(datetime(2026, 7, 6, 13, 30, 0, tzinfo=UTC), "18:30", "2026-07-06", "09:30") == 30


def test_overnight_overtime_uses_next_calendar_day_for_shift_end():
    # 06:40 IST on July 7; shift ended at 06:00 IST on July 7.
    assert compute_overtime(datetime(2026, 7, 7, 1, 10, 0, tzinfo=UTC), "06:00", "2026-07-06", "22:00") == 40


def test_work_hours_round_half_up():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert compute_work_hours(start, datetime(2026, 1, 1, 1, 0, 18, tzinfo=UTC)) == 1.01


def test_overnight_early_morning_punch_is_associated_with_previous_shift_day():
    # 05:55 IST on July 7 belongs to the shift that started July 6 at 22:00.
    assert compute_late_minutes(datetime(2026, 7, 7, 0, 25, 0, tzinfo=UTC), "22:00", "06:00", "2026-07-06") == 475
