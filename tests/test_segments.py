"""Unit tests for segment building, validation and file naming."""

import json

import pytest

from video_clipper.segments import (
    is_open_end,
    parse_ranges,
    resolve_open_ends,
    Segment,
    build_segments,
    load_csv,
    load_json,
    clip_filename,
    output_filename,
    parse_range_spec,
    source_ref,
    source_slug,
    span_timecode,
    validate_segments,
)


class TestParseRangeSpec:
    def test_equals_means_duration(self):
        seg = parse_range_spec("1:00=0:30")
        assert (seg.start, seg.end) == (60.0, 90.0)

    def test_equals_with_bare_seconds(self):
        seg = parse_range_spec("2:00=45")
        assert (seg.start, seg.end) == (120.0, 165.0)

    def test_dash_means_end(self):
        seg = parse_range_spec("0:00-:20")
        assert (seg.start, seg.end) == (0.0, 20.0)

    def test_zero_duration_is_refused(self):
        with pytest.raises(ValueError, match="больше нуля"):
            parse_range_spec("1:00=0")

    def test_new_separators_in_a_range(self):
        seg = parse_range_spec("1/12-1,30")
        assert (seg.start, seg.end) == (72.0, 90.0)

    def test_semicolon_chains_ranges(self):
        segs = parse_ranges("0:10-0:20; 2:00=45; 3:30-3:45")
        assert [(s.start, s.end) for s in segs] == [
            (10.0, 20.0),
            (120.0, 165.0),
            (210.0, 225.0),
        ]

    def test_semicolon_in_one_range_argument(self):
        segs = build_segments(ranges=["0:10-0:20; 1:00=30"])
        assert len(segs) == 2
    def test_simple(self):
        seg = parse_range_spec("1:12-1:45")
        assert (seg.start, seg.end, seg.duration) == (72.0, 105.0, 33.0)

    def test_full_timecodes(self):
        seg = parse_range_spec("00:00:10.5-00:00:20")
        assert seg.start == 10.5 and seg.end == 20.0

    def test_short_hand_seconds(self):
        """0:00-:30 from the chat is normalized to 0:00-0:30."""
        seg = parse_range_spec("0:00-:30")
        assert (seg.start, seg.end) == (0.0, 30.0)

    def test_missing_separator(self):
        with pytest.raises(ValueError, match="НАЧАЛО-КОНЕЦ"):
            parse_range_spec("1:12")

    def test_broken_timecode(self):
        with pytest.raises(ValueError):
            parse_range_spec("abc-def")


class TestLoadCsv:
    def test_without_header(self, tmp_path):
        f = tmp_path / "clips.csv"
        f.write_text("0:10,0:20\n1:00,1:30,intro\n", encoding="utf-8")
        segs = load_csv(f)
        assert [(s.start, s.end, s.name) for s in segs] == [
            (10.0, 20.0, None),
            (60.0, 90.0, "intro"),
        ]

    def test_with_header_and_comments(self, tmp_path):
        f = tmp_path / "clips.csv"
        f.write_text("# мои клипы\nstart,end,name\n0:05,0:10,\n", encoding="utf-8")
        segs = load_csv(f)
        assert len(segs) == 1 and segs[0].start == 5.0

    def test_empty_file(self, tmp_path):
        f = tmp_path / "clips.csv"
        f.write_text("# nothing\n", encoding="utf-8")
        with pytest.raises(ValueError, match="ни одного отрезка"):
            load_csv(f)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_csv(tmp_path / "nope.csv")


class TestLoadJson:
    def test_dicts(self, tmp_path):
        f = tmp_path / "clips.json"
        f.write_text(json.dumps([{"start": "0:01", "end": "0:02"}]), encoding="utf-8")
        assert load_json(f)[0].duration == 1.0

    def test_pairs_and_strings(self, tmp_path):
        f = tmp_path / "clips.json"
        f.write_text(json.dumps([["0:01", "0:02"], "0:10-0:12"]), encoding="utf-8")
        segs = load_json(f)
        assert [s.start for s in segs] == [1.0, 10.0]

    def test_not_a_list(self, tmp_path):
        f = tmp_path / "clips.json"
        f.write_text(json.dumps({"start": "0:01", "end": "0:02"}), encoding="utf-8")
        with pytest.raises(ValueError, match="JSON-массив"):
            load_json(f)


class TestBuildSegments:
    def test_from_ranges(self):
        segs = build_segments(ranges=["0:01-0:02", "0:10-0:12"])
        assert len(segs) == 2

    def test_combines_sources(self, tmp_path):
        f = tmp_path / "clips.csv"
        f.write_text("0:30,0:40\n", encoding="utf-8")
        segs = build_segments(ranges=["0:01-0:02"], csv_path=str(f))
        assert len(segs) == 2

    def test_no_segments(self):
        with pytest.raises(ValueError, match="Не задан ни один отрезок"):
            build_segments()

    def test_name_requires_single_segment(self):
        with pytest.raises(ValueError, match="только для одного отрезка"):
            build_segments(ranges=["0:01-0:02", "0:10-0:12"], name="intro")


class TestValidateSegments:
    def test_ok(self):
        validate_segments([Segment(1, 2), Segment(10, 12)], duration=60)

    def test_end_before_start(self):
        with pytest.raises(ValueError, match="должен быть больше начала"):
            validate_segments([Segment(10, 10)])

    def test_beyond_duration(self):
        with pytest.raises(ValueError, match="выходит за длительность"):
            validate_segments([Segment(1, 100)], duration=60)

    def test_overlap_rejected(self):
        with pytest.raises(ValueError, match="пересекаются"):
            validate_segments([Segment(1, 10), Segment(5, 12)])

    def test_overlap_allowed_by_flag(self):
        validate_segments([Segment(1, 10), Segment(5, 12)], allow_overlap=True)

    def test_empty(self):
        with pytest.raises(ValueError, match="пуст"):
            validate_segments([])


class TestNames:
    def test_default_name(self):
        seg = Segment(72.25, 105.5)
        assert output_filename("video", seg) == "video__000112-250__000145-500.mp4"

    def test_explicit_name(self):
        assert output_filename("video", Segment(1, 2), name="intro") == "intro.mp4"

    def test_name_from_segment(self):
        assert output_filename("video", Segment(1, 2, name="best moment")) == "best_moment.mp4"

    def test_name_keeps_mp4_suffix(self):
        assert output_filename("video", Segment(1, 2), name="clip.mp4") == "clip.mp4"

    def test_source_slug_local(self):
        assert source_slug("/home/alex/Videos/My Video.mp4") == "my_video"

    def test_source_slug_url(self):
        assert source_slug("https://youtu.be/dQw4w9WgXcQ") == "dqw4w9wgxcq"

    def test_source_slug_youtube_watch_uses_v_param(self):
        assert source_slug("https://www.youtube.com/watch?v=jNQXAC9IVRw") == "jnqxac9ivrw"
        assert source_slug("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=42s") == "jnqxac9ivrw"

    def test_source_slug_youtube_shorts(self):
        assert source_slug("https://www.youtube.com/shorts/AbCdEf12345") == "abcdef12345"

    def test_source_slug_tiktok(self):
        assert source_slug("https://www.tiktok.com/@user/video/7300000000000000000") == "7300000000000000000"

    def test_source_slug_cyrillic(self):
        assert source_slug("/tmp/Отпуск 2026.mp4") == "otpusk_2026"


class TestClipFilename:
    TITLE = "ПЛАН ГЛОБАЛИСТОВ ДО 2030 ГОДА: ДЕНЬГИ, ВОЙНЫ, ИИ. Денис Смык"
    URL = "https://www.youtube.com/watch?v=3UYFbmZwVes"

    def test_title_source_and_span(self):
        seg = Segment(29 * 60, 31 * 60 + 59)
        assert clip_filename(self.TITLE, self.URL, seg) == (
            "ПЛАН ГЛОБАЛИСТОВ ДО 2030 ГОДА ДЕНЬГИ, ВОЙНЫ, ИИ. Денис Смык"
            " - youtube.com-3UYFbmZwVes - [29:00-31:59].mp4"
        )

    def test_span_over_hour_keeps_hours(self):
        seg = Segment(3600 + 29 * 60, 3600 + 31 * 60 + 59)
        assert span_timecode(seg.start) == "1:29:00"
        assert "[1:29:00-1:31:59]" in clip_filename("T", self.URL, seg)

    def test_whole_video_span(self):
        assert clip_filename("Ролик", self.URL, Segment(0, 61), span="весь ролик") == (
            "Ролик - youtube.com-3UYFbmZwVes - [весь ролик].mp4"
        )

    def test_missing_title_falls_back_to_source(self):
        assert clip_filename(None, "https://youtu.be/dQw4w9WgXcQ", Segment(0, 5)) == (
            "youtu.be-dQw4w9WgXcQ - [00:00-00:05].mp4"
        )

    def test_local_file_named_like_title_drops_reference(self):
        assert clip_filename("Отпуск 2026", "/home/alex/Videos/Отпуск 2026.mp4", Segment(10, 20)) == (
            "Отпуск 2026 - [00:10-00:20].mp4"
        )

    def test_other_local_file_keeps_reference(self):
        assert clip_filename("Монтаж", "/home/alex/Videos/Отпуск 2026.mp4", Segment(10, 20)) == (
            "Монтаж - Отпуск 2026.mp4 - [00:10-00:20].mp4"
        )

    def test_suffix_marks_variant(self):
        assert clip_filename("Ролик", self.URL, Segment(0, 5), suffix="-fast") == (
            "Ролик - youtube.com-3UYFbmZwVes - [00:00-00:05]-fast.mp4"
        )

    def test_explicit_name_wins(self):
        assert clip_filename("Ролик", self.URL, Segment(0, 5), name="intro") == "intro.mp4"

    def test_forbidden_characters_removed(self):
        name = clip_filename('a/b:c\\d*e?f"g<h>i|j', self.URL, Segment(0, 5))
        title_part = name.split(" - ")[0]
        assert not set(title_part) & set('<>:"/\\|?*')

    def test_emoji_dropped_from_title(self):
        assert clip_filename("⚡️ПЛАН ГЛОБАЛИСТОВ", self.URL, Segment(0, 5)).startswith("ПЛАН ГЛОБАЛИСТОВ - ")

    def test_source_ref_for_tiktok(self):
        assert source_ref("https://www.tiktok.com/@user/video/7300000000000000000") == (
            "tiktok.com-7300000000000000000"
        )


class TestOpenEnd:
    def test_star_means_to_the_end(self):
        seg = parse_range_spec("20-*")
        assert seg.start == 20.0
        assert is_open_end(seg)

    def test_empty_right_side_is_open_too(self):
        seg = parse_range_spec("20-")
        assert is_open_end(seg) and seg.start == 20.0

    def test_word_конец_works(self):
        assert is_open_end(parse_range_spec("20-конец"))

    def test_open_end_is_not_an_error_without_duration(self):
        validate_segments([parse_range_spec("20-*")], duration=None)

    def test_open_end_is_not_rejected_by_duration(self):
        validate_segments([parse_range_spec("20-*")], duration=30.0)

    def test_resolve_fills_the_real_end(self):
        resolved = resolve_open_ends([parse_range_spec("20-*")], 95.5)
        assert resolved[0].end == 95.5 and not is_open_end(resolved[0])
        assert resolved[0].duration == 75.5

    def test_resolve_keeps_closed_segments(self):
        closed = Segment(1, 2)
        assert resolve_open_ends([closed], 100) == [closed]

    def test_resolve_without_duration_is_an_error(self):
        with pytest.raises(ValueError, match="длительность"):
            resolve_open_ends([parse_range_spec("20-*")], 0)

    def test_chain_with_open_end(self):
        segs = parse_ranges("0:00-0:05; 20-*")
        assert len(segs) == 2 and is_open_end(segs[1])

    def test_equals_still_means_duration(self):
        seg = parse_range_spec("20=5")
        assert seg.end == 25.0 and not is_open_end(seg)
