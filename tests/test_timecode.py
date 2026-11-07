"""Unit tests for timecode parsing and formatting."""

import pytest

from video_clipper.timecode import filename_timecode, format_timecode, parse_timecode


class TestParseTimecode:
    def test_seconds(self):
        assert parse_timecode("12") == 12.0
        assert parse_timecode("12.5") == 12.5

    def test_minutes_seconds(self):
        assert parse_timecode("1:12") == 72.0
        assert parse_timecode("01:12.250") == 72.25

    def test_hours_minutes_seconds(self):
        assert parse_timecode("01:02:03") == 3723.0
        assert parse_timecode("1:02:03.125") == 3723.125

    def test_comma_decimal(self):
        assert parse_timecode("1:12,5") == 72.5

    def test_numeric_input(self):
        assert parse_timecode(5) == 5.0
        assert parse_timecode(5.25) == 5.25

    def test_whitespace(self):
        assert parse_timecode("  1:12  ") == 72.0

    def test_leading_part_omitted(self):
        """":30" is the usual typo for 0:30, not a reason to refuse the request."""
        assert parse_timecode(":30") == 30.0
        assert parse_timecode(":05.5") == 5.5

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
