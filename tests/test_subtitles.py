"""Разбор субтитров и подготовка .srt под вырезанный отрезок."""

from __future__ import annotations

import pytest

from video_clipper.subtitles import (
    Cue,
    SubtitleError,
    cues_for_segment,
    detect_format,
    escape_for_ffmpeg,
    parse_srt,
    parse_subtitles,
    parse_vtt,
    render_srt,
    shift_cues,
    write_segment_srt,
)

SRT = """1
00:00:01,000 --> 00:00:04,000
Привет, это первая реплика

2
00:00:05,500 --> 00:00:09,250
<i>Вторая</i> реплика
с переносом строки

3
00:00:20,000 --> 00:00:24,000
Третья, уже вне отрезка
"""

VTT = """WEBVTT

NOTE это комментарий, его не читаем

cue-1
00:00:01.000 --> 00:00:04.000 align:middle line:90%
Первая реплика

00:00:05.500 --> 00:00:09.250
Вторая реплика
"""


class TestParsing:
    def test_srt_cues(self):
        cues = parse_srt(SRT)
        assert [round(c.start, 3) for c in cues] == [1.0, 5.5, 20.0]
        assert cues[1].text == "Вторая реплика\nс переносом строки"
        assert len(cues) == 3

    def test_indices_and_tags_are_stripped(self):
        cues = parse_srt(SRT)
        assert "<i>" not in cues[1].text
        assert not cues[0].text.startswith("1")

    def test_vtt_header_note_and_settings(self):
        cues = parse_vtt(VTT)
        assert len(cues) == 2, "заголовок WEBVTT и NOTE не реплики"
        assert round(cues[0].end, 3) == 4.0, "настройки после таймкода не мешают"
        assert cues[0].text == "Первая реплика"

    def test_format_detected_by_content(self):
        assert detect_format(VTT) == "vtt"
        assert detect_format(SRT) == "srt"

    def test_broken_time_is_skipped_not_fatal(self):
        text = "1\n00:00:xx,000 --> 00:00:04,000\nмусор\n\n2\n00:00:05,000 --> 00:00:06,000\nок\n"
        assert [c.text for c in parse_srt(text)] == ["ок"]

    def test_empty_raises(self):
        with pytest.raises(SubtitleError):
            parse_subtitles("/nonexistent/file.srt")

    def test_cp1251_file_is_readable(self, tmp_path):
        path = tmp_path / "subs.srt"
        path.write_bytes(SRT.encode("cp1251"))
        cues = parse_subtitles(path)
        assert cues[0].text.startswith("Привет")


class TestSegment:
    def test_cues_are_shifted_to_zero_and_clipped(self):
        cues = parse_srt(SRT)
        got = cues_for_segment(cues, 5.0, 20.5)
        assert [round(c.start, 2) for c in got] == [0.5, 15.0]
        assert round(got[0].end, 2) == 4.25
        assert round(got[1].end, 2) == 15.5, "реплика обрезана концом отрезка"

    def test_shift_can_be_negative(self):
        got = shift_cues([Cue(10.0, 12.0, "x")], -4.0)
        assert (got[0].start, got[0].end) == (6.0, 8.0)

    def test_render_srt_keeps_order_and_format(self):
        text = render_srt([Cue(0.5, 4.25, "раз\nдва")])
        assert text.startswith("1\n00:00:00,500 --> 00:00:04,250\nраз\nдва")

    def test_write_segment_srt(self, tmp_path):
        subs = tmp_path / "source.srt"
        subs.write_text(SRT, encoding="utf-8")
        # конец 19.5: третья реплика начинается в 20.0 и в отрезок не попадает
        out = write_segment_srt(subs, tmp_path / "clip.srt", 5.0, 19.5)
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "00:00:00,500" in body and "Третья" not in body

    def test_segment_without_cues_raises(self, tmp_path):
        subs = tmp_path / "source.srt"
        subs.write_text(SRT, encoding="utf-8")
        with pytest.raises(SubtitleError):
            write_segment_srt(subs, tmp_path / "clip.srt", 30.0, 40.0)


class TestFfmpegPath:
    def test_special_characters_are_escaped(self):
        escaped = escape_for_ffmpeg("/tmp/папка с пробелом/a:b's.srt")
        assert "\\:" in escaped and "\\'" in escaped
        assert escaped.startswith("/")
