from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from .catalog import load_catalog
from .config import load_config
from .jellyfin import notify_jellyfin
from .m3u import parse_selected_vod_catalog, parse_series_episodes
from .tmdb import enrich_with_tmdb
from .writer import write_movies, write_series
from .xtream import XtreamClient

PROGRESS_PREFIX = "__VOD_STRM_BUILDER_PROGRESS__ "
ProgressCallback = Callable[[dict[str, object]], None]


def existing_tmdb_stats(movies: list[object], series: list[object]) -> dict[str, int]:
    return {
        "movies_with_provider_tmdb_id": sum(1 for item in movies if getattr(item, "tmdb_id", None)),
        "series_with_provider_tmdb_id": sum(1 for item in series if getattr(item, "tmdb_id", None)),
    }


def selected_category_ids(categories: dict[str, str], names: set[str], ids: set[str]) -> set[str]:
    resolved = {str(category_id) for category_id in ids}
    selected_names = {name.strip() for name in names}
    for category_id, category_name in categories.items():
        if category_name in selected_names:
            resolved.add(str(category_id))
    return resolved


def can_use_m3u_catalog(config) -> bool:
    return bool(
        config.provider.m3u_file
        and not config.filters.movie_category_ids
        and not config.filters.series_category_ids
    )


def load_provider_catalog(config, client: XtreamClient, progress: ProgressCallback | None = None):
    emit_progress(progress, "Loading categories", 5)
    movie_categories = client.categories("movie")
    series_categories = client.categories("series")
    emit_progress(progress, "Loading movie catalog", 10)
    movie_ids = selected_category_ids(movie_categories, config.filters.movie_groups, config.filters.movie_category_ids)
    series_ids = selected_category_ids(series_categories, config.filters.series_groups, config.filters.series_category_ids)

    all_movies = client.movies()
    emit_progress(progress, "Loading series catalog", 20)
    all_series = client.series()
    movies = [item for item in all_movies if item_matches_category(item, movie_ids)]
    series = [item for item in all_series if item_matches_category(item, series_ids)]
    emit_progress(progress, "Catalog loaded", 30)

    return movies, series, {
        "catalog_source": "xtream_api",
        "selected_movie_categories": len(movie_ids),
        "selected_series_categories": len(series_ids),
        "provider_movies_seen": len(all_movies),
        "provider_series_seen": len(all_series),
        "movies_selected": len(movies),
        "series_selected": len(series),
    }


def load_m3u_catalog(config, client: XtreamClient, progress: ProgressCallback | None = None):
    emit_progress(progress, "Scanning playlist catalog", 5)
    movies, series, stats = parse_selected_vod_catalog(
        client.m3u_source(),
        config.filters.movie_groups,
        config.filters.series_groups,
        config.provider.user_agent,
        progress=byte_progress(progress, 5, 30, "Scanning playlist catalog"),
    )
    emit_progress(progress, "Playlist catalog loaded", 30)
    return movies, series, {
        "catalog_source": "m3u",
        "movies_selected": len(movies),
        "series_selected": len(series),
        "m3u_catalog_parse": stats._asdict(),
    }


def generate(config_path: str, progress: ProgressCallback | None = None) -> dict[str, object]:
    emit_progress(progress, "Loading config", 1)
    config = load_config(config_path)
    client = XtreamClient(config.provider)

    if config.catalog_file:
        emit_progress(progress, "Loading catalog file", 5)
        movies, series, catalog_metadata = load_catalog(config.catalog_file)
        summary: dict[str, object] = {
            "catalog_file": str(config.catalog_file),
            "catalog_metadata": catalog_metadata,
            "movies_selected": len(movies),
            "series_selected": len(series),
        }
        emit_progress(progress, "Catalog loaded", 30)
    else:
        if can_use_m3u_catalog(config):
            movies, series, summary = load_m3u_catalog(config, client, progress)
        else:
            movies, series, summary = load_provider_catalog(config, client, progress)

    provider_tmdb_stats = existing_tmdb_stats(movies, series)
    summary.update(provider_tmdb_stats)
    emit_progress(progress, "Resolving TMDB IDs", 30)
    movies, series, tmdb_stats = enrich_with_tmdb(
        config,
        movies,
        series,
        progress=count_progress(progress, 30, 55, "Resolving TMDB IDs"),
    )
    summary.update(tmdb_stats)
    if (
        config.output.append_tmdb_id
        and not config.tmdb.enabled
        and provider_tmdb_stats["movies_with_provider_tmdb_id"] == 0
        and provider_tmdb_stats["series_with_provider_tmdb_id"] == 0
    ):
        summary.setdefault("warnings", []).append(
            "append_tmdb_id is enabled, but the selected catalog has no TMDB IDs and TMDB lookup fallback is disabled."
        )
    emit_progress(progress, "Writing movie files", 55)
    summary.update(write_movies(config, client, movies, progress=count_progress(progress, 55, 68, "Writing movie files")))

    if not series:
        episodes = []
        if config.series.source == "api":
            summary["api_series_parse"] = {"series_checked": 0, "series_failed": 0, "episodes_emitted": 0}
        elif config.series.source == "m3u":
            summary["m3u_series_parse"] = {
                "seen_urls": 0,
                "parsed_titles": 0,
                "emitted": 0,
                "skipped_group": 0,
                "unmapped": 0,
                "ambiguous": 0,
            }
        else:
            raise SystemExit(f"Unknown series.source={config.series.source!r}; use 'm3u' or 'api'.")
        emit_progress(progress, "No selected series to scan", 88)
    elif config.series.source == "api":
        episodes = []
        series_api_stats = {"series_checked": 0, "series_failed": 0, "episodes_emitted": 0}
        emit_progress(progress, "Fetching series episodes", 68)
        total_series = len(series)
        for index, item in enumerate(series, start=1):
            series_api_stats["series_checked"] += 1
            try:
                item_episodes = client.series_episodes(item)
            except RuntimeError:
                series_api_stats["series_failed"] += 1
                continue
            episodes.extend(item_episodes)
            series_api_stats["episodes_emitted"] += len(item_episodes)
            emit_progress(
                progress,
                f"Fetching series episodes {index:,}/{total_series:,}" if total_series else "Fetching series episodes",
                scale_percent(68, 88, index, total_series),
                current=index,
                total=total_series,
                unit="series",
            )
        summary["api_series_parse"] = series_api_stats
    elif config.series.source == "m3u":
        emit_progress(progress, "Scanning series episodes", 68)
        episodes, stats = parse_series_episodes(
            client.m3u_source(),
            series,
            config.filters.series_groups,
            config.provider.user_agent,
            config.series.require_selected_m3u_group,
            config.series.quality_words,
            progress=byte_progress(progress, 68, 88, "Scanning series episodes"),
        )
        summary["m3u_series_parse"] = stats._asdict()
    else:
        raise SystemExit(f"Unknown series.source={config.series.source!r}; use 'm3u' or 'api'.")
    emit_progress(progress, "Writing series files", 88)
    summary.update(write_series(config, episodes, progress=count_progress(progress, 88, 98, "Writing series files")))
    emit_progress(progress, "Notifying Jellyfin", 99)
    summary.update(notify_jellyfin(config.jellyfin, config.output.dry_run))
    emit_progress(progress, "Provider complete", 100)
    return summary


def emit_progress(progress: ProgressCallback | None, label: str, percent: float | int | None, **extra: object) -> None:
    if not progress:
        return
    event = {"label": label, **extra}
    if percent is not None:
        event["percent"] = round(max(0.0, min(100.0, float(percent))), 1)
    progress(event)


def scale_percent(start: float, end: float, current: int | float, total: int | float | None) -> float:
    if not total or total <= 0:
        return end
    ratio = max(0.0, min(1.0, float(current) / float(total)))
    return start + ((end - start) * ratio)


def byte_progress(progress: ProgressCallback | None, start: float, end: float, label: str):
    def report(current: int, total: int | None) -> None:
        percent = scale_percent(start, end, current, total) if total else None
        emit_progress(progress, label, percent, current=current, total=total or 0, unit="bytes")

    return report


def count_progress(progress: ProgressCallback | None, start: float, end: float, label: str):
    def report(current: int, total: int) -> None:
        suffix = f" {current:,}/{total:,}" if total else ""
        emit_progress(
            progress,
            f"{label}{suffix}",
            scale_percent(start, end, current, total),
            current=current,
            total=total,
            unit="items",
        )

    return report


def item_matches_category(item: object, category_ids: set[str]) -> bool:
    ids = getattr(item, "category_ids", None) or ()
    values = {str(value) for value in ids}
    primary = getattr(item, "category_id", "")
    if primary:
        values.add(str(primary))
    return bool(values & category_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate STRM/NFO VOD libraries from Xtream/M3U sources.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate movie and series libraries.")
    generate_parser.add_argument("--config", required=True, help="Path to config YAML.")
    generate_parser.add_argument("--summary-json", help="Optional path to write the generation summary JSON.")
    generate_parser.add_argument("--progress-jsonl", action="store_true", help="Emit machine-readable progress lines.")

    args = parser.parse_args()
    if args.command == "generate":
        progress = None
        if args.progress_jsonl:
            def progress(event: dict[str, object]) -> None:
                print(PROGRESS_PREFIX + json.dumps(event, sort_keys=True), flush=True)

        summary = generate(args.config, progress=progress)
        text = json.dumps(summary, indent=2, sort_keys=True)
        print(text)
        if args.summary_json:
            Path(args.summary_json).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
