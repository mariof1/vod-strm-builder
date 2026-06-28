from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .models import AppConfig, EpisodeItem, MovieItem, SeriesItem
from .nfo import episode_nfo, movie_nfo, series_nfo
from .utils import ensure_empty_dir, folder_name, safe_filename, strip_redundant_year, clean_title
from .xtream import XtreamClient


def write_text(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_movies(config: AppConfig, client: XtreamClient, movies: list[MovieItem]) -> dict[str, int]:
    if config.output.clean:
        ensure_empty_dir(config.output.movies_dir, config.output.dry_run)
    written_paths: set[Path] = set()
    written = skipped_duplicate = 0
    for item in movies:
        folder = config.output.movies_dir / folder_name(
            item.name,
            item.year,
            item.tmdb_id,
            config.output.append_tmdb_id,
        )
        title = strip_redundant_year(clean_title(item.name), item.year)
        filename = safe_filename(f"{title} ({item.year})" if item.year else title)
        strm_path = folder / f"{filename}.strm"
        if strm_path in written_paths:
            skipped_duplicate += 1
            continue
        write_text(strm_path, client.movie_url(item), config.output.dry_run)
        if config.output.generate_nfo:
            write_text(folder / f"{filename}.nfo", movie_nfo(item), config.output.dry_run)
        written_paths.add(strm_path)
        written += 1
    return {"movies_written": written, "movie_duplicates_skipped": skipped_duplicate}


def write_series(config: AppConfig, episodes: list[EpisodeItem]) -> dict[str, int]:
    if config.output.clean:
        ensure_empty_dir(config.output.series_dir, config.output.dry_run)
    grouped: dict[str, list[EpisodeItem]] = defaultdict(list)
    for ep in episodes:
        grouped[ep.series.series_id].append(ep)

    series_written = episode_written = duplicate_paths = 0
    written_paths: set[Path] = set()
    for eps in grouped.values():
        series = eps[0].series
        series_folder = config.output.series_dir / folder_name(
            series.name,
            series.year,
            series.tmdb_id,
            config.output.append_tmdb_id,
        )
        if config.output.generate_nfo:
            write_text(series_folder / "tvshow.nfo", series_nfo(series), config.output.dry_run)
        wrote_any = False
        for ep in sorted(eps, key=lambda item: (item.season, item.episode, item.stream_id)):
            title = safe_filename(f"{clean_title(series.name)} - S{ep.season:02d}E{ep.episode:02d} - {ep.title}")
            path = series_folder / f"Season {ep.season:02d}" / f"{title}.strm"
            if path in written_paths:
                duplicate_paths += 1
                continue
            write_text(path, ep.url, config.output.dry_run)
            if config.output.generate_nfo:
                write_text(path.with_suffix(".nfo"), episode_nfo(ep), config.output.dry_run)
            written_paths.add(path)
            episode_written += 1
            wrote_any = True
        if wrote_any:
            series_written += 1
    return {
        "series_written": series_written,
        "episodes_written": episode_written,
        "episode_duplicates_skipped": duplicate_paths,
    }

