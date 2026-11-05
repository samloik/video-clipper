"""Command-line interface for video-clipper."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .probe import EnvironmentProblem, check_tools

log = logging.getLogger("video_clipper")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video-clipper",
        description="Вырезает фрагменты видео по таймкодам из URL (YouTube, TikTok, VK, ...) или локального файла.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  video-clipper https://youtu.be/ID --range 1:12-1:45\n"
            "  video-clipper video.mp4 --range 0:10-0:25 --range 2:00-2:30 --name intro\n"
            "  video-clipper https://youtu.be/ID --csv clips.csv --fast\n"
            "  video-clipper video.mp4 --range 0:10-0:25 --vertical   # 9:16 для смартфона\n"
            "  video-clipper video.mp4 --range 0:10-0:25 --blur       # 9:16 + размытый фон\n"
            "  video-clipper --gui\n"
        ),
    )
    p.add_argument("source", nargs="?", help="URL видео или путь к локальному файлу")
    p.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        metavar="START-END",
        help="отрезок в формате НАЧАЛО-КОНЕЦ, например 1:12-1:45 (можно несколько раз)",
    )
    p.add_argument("--csv", help="файл со списком отрезков: start,end[,name]")
    p.add_argument("--json", dest="json_segments", help="JSON-массив отрезков: [{start,end,name}]")
    p.add_argument("--name", help="имя выходного файла (только для одного отрезка)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--accurate", dest="fast", action="store_false", help="точная нарезка с перекодированием (по умолчанию)")
    mode.add_argument("--fast", dest="fast", action="store_true", help="без перекодирования, точность до ключевого кадра")
    p.set_defaults(fast=False)
    p.add_argument("--allow-overlap", action="store_true", help="разрешить пересекающиеся отрезки")
    p.add_argument(
        "--vertical",
        action="store_true",
        help="вертикальный кадр 9:16 для смартфона (центр-кроп 1080x1920)",
    )
    p.add_argument(
        "--blur",
        action="store_true",
        help="вертикальный кадр с размытым фоном вместо обрезки (включает --vertical)",
    )
    p.add_argument("--keep-source", action="store_true", help="сохранить скачанный оригинал в cache/")
    p.add_argument("--output-dir", help="каталог для готовых фрагментов")
    p.add_argument("--resolution", type=int, help="максимальное разрешение при скачивании, например 720")
    p.add_argument("--cookies", help="путь к cookies.txt для yt-dlp")
    p.add_argument("--proxy", help="прокси для yt-dlp, например socks5://127.0.0.1:10808")
    p.add_argument("--json-report", help="записать отчёт о нарезках в JSON-файл")
    p.add_argument("--config", help="путь к config.json")
    p.add_argument("--dry-run", action="store_true", help="показать план нарезки без запуска ffmpeg")
    p.add_argument("--gui", action="store_true", help="запустить локальный веб-GUI")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    p.add_argument("--version", action="version", version=f"video-clipper {__version__}")
    return p


def setup_logging(log_file: str | Path, verbose: bool = False, quiet: bool = False) -> None:
    handlers: list[logging.Handler] = []
    if not quiet:
        handlers.append(logging.StreamHandler(sys.stderr))
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from .gui import run_gui

        return run_gui(config_path=args.config)

    cfg = load_config(args.config).with_overrides(
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        default_resolution=args.resolution,
        cookies=args.cookies,
        proxy=args.proxy,
        keep_source=True if args.keep_source else None,
    )
    setup_logging(cfg.log_file, verbose=args.verbose)

    try:
        versions = check_tools(cfg.ffmpeg_path, cfg.ffprobe_path)
    except EnvironmentProblem as exc:
        print(f"Ошибка окружения: {exc}", file=sys.stderr)
        return 2
    log.debug("tools: %s", versions)

    if not args.source:
        parser.error("укажи URL или файл (либо используй --gui)")
    if not (args.ranges or args.csv or args.json_segments):
        parser.error("нужен хотя бы один отрезок: --range, --csv или --json")

    from .pipeline import run_pipeline

    try:
        results = run_pipeline(args, cfg)
    except (ValueError, EnvironmentProblem, FileNotFoundError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Прервано пользователем", file=sys.stderr)
        return 130

    if args.json_report:
        from .report import write_json_report

        write_json_report(
            args.json_report, results, source=str(args.source), dry_run=bool(args.dry_run)
        )

    for res in results:
        print(res.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
