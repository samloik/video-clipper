"""Выжигание субтитров: где стоит фильтр и как он одет."""

from __future__ import annotations

from pathlib import Path

from video_clipper.cut import (
    build_ffmpeg_command,
    subtitle_filter,
    subtitle_style,
    vertical_args,
)
from video_clipper.segments import Segment


def _cmd(**kwargs):
    return build_ffmpeg_command(
        "/tmp/src.mp4", "/tmp/out.mp4", Segment(1.0, 6.0), **kwargs
    )


def _value_after(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


class TestStyle:
    def test_size_is_in_script_units_not_pixels(self):
        """libass считает FontSize в единицах 288, иначе буквы во весь кадр."""
        horizontal = subtitle_style(360)
        vertical = subtitle_style(1280, vertical=True)
        assert "FontSize=14," in horizontal
        assert "FontSize=22," in vertical, "на 9:16 шрифт крупнее"
        assert "FontName=DejaVu Sans" in horizontal

    def test_margin_keeps_text_off_the_edge(self):
        assert "MarginV=23" in subtitle_style(360)
        assert "MarginV=32" in subtitle_style(1280, vertical=True)
        assert "Outline=2" in subtitle_style(), "без обводки текст пропадает на светлом кадре"


class TestBurnFilters:
    def test_horizontal_gets_a_plain_vf(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subtitle_height=360)
        vf = _value_after(cmd, "-vf")
        assert vf.startswith("subtitles=filename=")
        assert "-filter_complex" not in cmd

    def test_burning_never_copies_the_stream(self):
        """С выжженным текстом --fast невозможен: кадр обязан перекодироваться."""
        cmd = _cmd(fast=True, subtitles="/tmp/sub.srt", subtitle_height=360)
        assert "-c" not in cmd or "copy" not in cmd
        assert any("libx264" in part for part in cmd), "должно быть перекодирование"

    def test_vertical_crops_first_then_burns(self):
        args = vertical_args("crop", (720, 1280), 0.5, burn="subtitles=filename='x'")
        assert args[0] == "-vf"
        chain = args[1]
        assert chain.index("crop=") < chain.index("subtitles="), "текст после кропа"
        assert chain.endswith("subtitles=filename='x'")

    def test_vertical_blur_labels_the_result_before_burning(self):
        args = vertical_args("blur", (720, 1280), 0.5, burn="subtitles=filename='x'")
        assert args[0] == "-filter_complex"
        chain = args[1]
        assert "[base]subtitles=filename='x'" in chain
        assert chain.count("setsar=1") == 1

    def test_command_for_vertical_blur_with_subs(self):
        cmd = _cmd(
            vertical=True,
            layout="blur",
            vertical_size=(720, 1280),
            subtitles="/tmp/sub.srt",
        )
        chain = _value_after(cmd, "-filter_complex")
        assert "gblur" in chain and "subtitles=" in chain
        assert chain.index("overlay=") < chain.index("subtitles="), "текст поверх подложки"


class TestStartTime:
    def test_no_negative_ts_flag_in_accurate_mode(self):
        """Флаг сдвигал клип на 80 мс: start_time 0.080 вместо 0.000."""
        for kwargs in ({}, {"subtitles": "/tmp/sub.srt"}, {"subtitles": "/tmp/sub.srt", "subs_mode": "soft"},
                       {"vertical": True, "layout": "blur", "vertical_size": (720, 1280)}):
            cmd = _cmd(**kwargs)
            assert "-avoid_negative_ts" not in cmd, kwargs


class TestPathEscaping:
    def test_colon_and_quotes_are_escaped(self):
        expr = subtitle_filter("/tmp/a:b/c'd.srt")
        assert "\\:" in expr and "\\'" in expr

    def test_style_is_passed_through(self):
        expr = subtitle_filter("/tmp/x.srt", "FontSize=20")
        assert expr.endswith("force_style='FontSize=20'")


class TestSoftTrack:
    """Мягкая дорожка: текст отдельным потоком, картинка не перекодируется."""

    def test_soft_adds_a_second_input_and_maps_tracks(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft")
        assert cmd.count("-i") == 2, "видео и файл субтитров"
        assert cmd[cmd.index("/tmp/sub.srt") - 1] == "-i"
        assert ["-map", "0:v:0"] == cmd[cmd.index("-map") : cmd.index("-map") + 2]
        assert "-map" in cmd and "1:0" in cmd
        assert "mov_text" in cmd

    def test_soft_does_not_burn_anything_into_the_picture(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft")
        assert "-vf" not in cmd, "фильтра выжигания быть не должно"
        assert "-filter_complex" not in cmd

    def test_soft_with_fast_copies_the_video(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft", fast=True)
        assert "-c" in cmd and _value_after(cmd, "-c") == "copy"
        assert _value_after(cmd, "-c:s") == "mov_text", "субтитры всё равно упаковываем"

    def test_soft_without_fast_re_encodes_but_keeps_subtitles(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft")
        assert _value_after(cmd, "-c:v") == "libx264"
        assert _value_after(cmd, "-c:s") == "mov_text"

    def test_soft_in_vertical_keeps_crop_and_subtitle_track(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft", vertical=True,
                   layout="crop", vertical_size=(720, 1280))
        chain = _value_after(cmd, "-vf")
        assert "crop=" in chain
        assert "subtitles=" not in " ".join(cmd), "в мягком режиме текст не рисуется в кадр"
        assert _value_after(cmd, "-c:s") == "mov_text"

    def test_subtitle_input_comes_before_t(self):
        """-t после -i субтитров стал бы его опцией и обрезал бы дорожку."""
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft")
        assert cmd.index("/tmp/sub.srt") < cmd.index("-t")

    def test_track_is_default_and_named(self):
        cmd = _cmd(subtitles="/tmp/sub.srt", subs_mode="soft")
        assert ["-disposition:s:0", "default"] == cmd[
            cmd.index("-disposition:s:0") : cmd.index("-disposition:s:0") + 2
        ]
        assert "title=Субтитры" in cmd

    def test_burn_mode_is_still_the_default(self):
        cmd = _cmd(subtitles="/tmp/sub.srt")
        assert cmd.count("-i") == 1
        assert "subtitles=filename=" in _value_after(cmd, "-vf")
        assert "mov_text" not in cmd

    def test_mode_string_tells_soft_from_burn(self):
        from video_clipper.cut import clip_mode

        assert clip_mode(True, False, "crop", "/tmp/s.srt", "soft") == "fast+subs-soft"
        assert clip_mode(True, False, "crop", "/tmp/s.srt", "burn") == "accurate+subs"
        assert clip_mode(True, True, "blur", "/tmp/s.srt", "soft") == "accurate+vertical-blur+subs-soft"
        assert clip_mode(True, False, "crop", None, "burn") == "fast"
