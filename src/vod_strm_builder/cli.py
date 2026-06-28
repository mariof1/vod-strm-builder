from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import load_catalog
from .config import load_config
from .jellyfin import notify_jellyfin
from .m3u import parse_series_episodes
from .tmdb import enrich_with_tmdb
from .writer import write_movies, write_series
from .xtream import XtreamClient


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


def generate(config_path: str) -> dict[str, object]:
    config = load_config(config_path)
    client = XtreamClient(config.provider)

    if config.catalog_file:
        movies, series, catalog_metadata = load_catalog(config.catalog_file)
        summary: dict[str, object] = {
            "catalog_file": str(config.catalog_file),
            "catalog_metadata": catalog_metadata,
            "movies_selected": len(movies),
            "series_selected": len(series),
        }
    else:
        movie_categories = client.categories("movie")
        series_categories = client.categories("series")
        movie_ids = selected_category_ids(movie_categories, config.filters.movie_groups, config.filters.movie_category_ids)
        series_ids = selected_category_ids(series_categories, config.filters.series_groups, config.filters.series_category_ids)

        all_movies = client.movies()
        all_series = client.series()
        movies = [item for item in all_movies if str(item.category_id) in movie_ids]
        series = [item for item in all_series if str(item.category_id) in series_ids]

        summary = {
            "selected_movie_categories": len(movie_ids),
            "selected_series_categories": len(series_ids),
            "provider_movies_seen": len(all_movies),
            "provider_series_seen": len(all_series),
            "movies_selected": len(movies),
            "series_selected": len(series),
        }

    summary.update(existing_tmdb_stats(movies, series))
    movies, series, tmdb_stats = enrich_with_tmdb(config, movies, series)
    summary.update(tmdb_stats)
    summary.update(write_movies(config, client, movies))

    if config.series.source == "api":
        episodes = []
        series_api_stats = {"series_checked": 0, "series_failed": 0, "episodes_emitted": 0}
        for item in series:
            series_api_stats["series_checked"] += 1
            try:
                item_episodes = client.series_episodes(item)
            except RuntimeError:
                series_api_stats["series_failed"] += 1
                continue
            episodes.extend(item_episodes)
            series_api_stats["episodes_emitted"] += len(item_episodes)
        summary["api_series_parse"] = series_api_stats
    elif config.series.source == "m3u":
        episodes, stats = parse_series_episodes(
            client.m3u_source(),
            series,
            config.filters.series_groups,
            config.provider.user_agent,
            config.series.require_selected_m3u_group,
            config.series.quality_words,
        )
        summary["m3u_series_parse"] = stats._asdict()
    else:
        raise SystemExit(f"Unknown series.source={config.series.source!r}; use 'm3u' or 'api'.")
    summary.update(write_series(config, episodes))
    summary.update(notify_jellyfin(config.jellyfin, config.output.dry_run))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate STRM/NFO VOD libraries from Xtream/M3U sources.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate movie and series libraries.")
    generate_parser.add_argument("--config", required=True, help="Path to config YAML.")
    generate_parser.add_argument("--summary-json", help="Optional path to write the generation summary JSON.")

    args = parser.parse_args()
    if args.command == "generate":
        summary = generate(args.config)
        text = json.dumps(summary, indent=2, sort_keys=True)
        print(text)
        if args.summary_json:
            Path(args.summary_json).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
