"""Unit tests for timecode parsing and formatting."""

import pytest

from video_clipper.timecode import (
    filename_timecode,
    format_moment,
    format_retention,
    format_timecode,
    parse_retention,
    parse_timecode,
)


class TestParseTimecode:
    def test_seconds(self):
        assert parse_timecode("12") == 12.0
        assert parse_timecode("45") == 45.0

    def test_two_parts_are_minutes_and_seconds(self):
        """Hours are never assumed: "34:43" is 34 min 43 s, not 34 h 43 min."""
        assert parse_timecode("34:43") == 34 * 60 + 43
        # точка — доли секунды, поэтому «12.5» это 12.5 с, а не 12:05
        assert parse_timecode("12.5") == 12.5
        assert parse_timecode("12,5") == 12 * 60 + 5

    def test_minutes_seconds(self):
        assert parse_timecode("1:12") == 72.0
        assert parse_timecode("01:12.250") == 72.25

    def test_hours_minutes_seconds(self):
        assert parse_timecode("01:02:03") == 3723.0
        assert parse_timecode("1:02:03.125") == 3723.125

    @pytest.mark.parametrize("text", ["1:12", "1,12", "1/12", "1\\12"])
    def test_any_separator_splits_the_parts(self, text):
        """,", "/" and "\\" work exactly like ":"; a dot does not."""
        assert parse_timecode(text) == 72.0

    def test_a_dot_is_always_a_fraction(self):
        """Точка — доли секунды, а не граница частей (это просил пользователь)."""
        assert parse_timecode("1.12") == 1.12
        assert parse_timecode("0:29.1") == 29.1

    def test_four_digits_after_the_dot_are_a_typo(self):
        """Раньше шаг кадра давал «0:29.1000», и это читалось как 29 мин 1000 с."""
        with pytest.raises(ValueError) as exc:
            parse_timecode("0:29.1000")
        assert "до трёх цифр" in str(exc.value)
        with pytest.raises(ValueError):
            parse_timecode("1.2500")

    def test_comma_splits_like_a_colon(self):
        assert parse_timecode("1:12,5") == 1 * 3600 + 12 * 60 + 5

    @pytest.mark.parametrize(
        "text, expected",
        [("1:12.500", 72.5), ("00:00:10.5", 10.5), ("0.500", 0.5), ("0.30", 0.3)],
    )
    def test_fraction_needs_three_digits_or_a_full_timecode(self, text, expected):
        assert parse_timecode(text) == expected

    def test_numeric_input(self):
        assert parse_timecode(5) == 5.0
        assert parse_timecode(5.25) == 5.25

    def test_whitespace(self):
        assert parse_timecode("  1:12  ") == 72.0

    def test_leading_part_omitted(self):
        """":30" is the usual typo for 0:30, not a reason to refuse the request."""
        assert parse_timecode(":30") == 30.0
        assert parse_timecode(":05") == 5.0
        assert parse_timecode("/30") == 30.0

    def test_trailing_part_omitted(self):
        """A colon with nothing after it means zero seconds."""
        assert parse_timecode("1:") == 60.0
        assert parse_timecode("1:02:") == 3720.0

    def test_middle_part_omitted_is_rejected(self):
        with pytest.raises(ValueError):
            parse_timecode("1::30")

    @pytest.mark.parametrize("bad", [":", "::"])
    def test_only_separators_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_timecode(bad)

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "1:ab", "1:2:3:4", "!!!", "-5", "--1:00"])
    def test_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            parse_timecode(bad)

    @pytest.mark.parametrize("bad", ["1:75", "0:99", "1:99:00", "1:00:75"])
    def test_rejects_out_of_range_parts(self, bad):
        with pytest.raises(ValueError):
            parse_timecode(bad)

    def test_rejects_negative_number(self):
        with pytest.raises(ValueError):
            parse_timecode(-1)
        with pytest.raises(ValueError):
            parse_timecode(-1.5)

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            parse_timecode(True)


class TestFormatTimecode:
    def test_format_with_millis(self):
        assert format_timecode(72.25) == "00:01:12.250"

    def test_format_without_millis(self):
        assert format_timecode(3723) == "01:02:03"

    def test_roundtrip(self):
        for seconds in (0, 5.5, 72.25, 3723.125, 35999.999):
            assert parse_timecode(format_timecode(seconds)) == pytest.approx(seconds, abs=1e-3)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            format_timecode(-1)


class TestFilenameTimecode:
    def test_without_millis(self):
        assert filename_timecode(72) == "000112"

    def test_with_millis(self):
        assert filename_timecode(72.25) == "000112-250"

    def test_rounding_to_next_second(self):
        assert filename_timecode(59.9999) == "000100"


class TestParseRetention:
    def test_bare_number_is_minutes(self):
        assert parse_retention("30") == 30
        assert parse_retention("45") == 45

    def test_hours_and_minutes(self):
        assert parse_retention("1:30") == 90
        assert parse_retention("0:30") == 30

    def test_suffixes(self):
        assert parse_retention("2ч") == 120
        assert parse_retention("20 мин") == 20
        assert parse_retention("1 час") == 60

    def test_other_separators(self):
        assert parse_retention("1/30") == 90
        assert parse_retention("1.30") == 90

    def test_zero_and_garbage_are_rejected(self):
        with pytest.raises(ValueError):
            parse_retention("0")
        with pytest.raises(ValueError):
            parse_retention("")
        with pytest.raises(ValueError):
            parse_retention("полчаса")

    def test_too_long_is_rejected(self):
        with pytest.raises(ValueError, match="максимум"):
            parse_retention("200:00")


class TestFormatRetention:
    def test_minutes_only(self):
        assert format_retention(30) == "30 мин"

    def test_hours(self):
        assert format_retention(90) == "1 ч 30 мин"
        assert format_retention(120) == "2 ч"

    def test_moment_has_date_and_time(self):
        text = format_moment(1794048358)
        assert text.count(".") == 1 and " в " in text
