"""Выжигание субтитров: где стоит фильтр и как он одет."""

from __future__ import annotations

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


class TestPathEscaping:
    def test_colon_and_quotes_are_escaped(self):
        expr = subtitle_filter("/tmp/a:b/c'd.srt")
        assert "\\:" in expr and "\\'" in expr

    def test_style_is_passed_through(self):
        expr = subtitle_filter("/tmp/x.srt", "FontSize=20")
        assert expr.endswith("force_style='FontSize=20'")
