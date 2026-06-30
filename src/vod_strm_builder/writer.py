from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Callable, Any

from .models import AppConfig, EpisodeItem, MovieItem, SeriesItem
from .nfo import episode_nfo, movie_nfo, series_nfo
from .utils import ensure_empty_dir, folder_name, safe_filename, strip_redundant_year, clean_title
from .xtream import XtreamClient


def write_text(path: Path, content: str, dry_run: bool, skip_unchanged: bool = True) -> str:
    if dry_run:
        return "dry_run"
    if skip_unchanged and path.exists():
        try:
            if path.read_text(encoding="utf-8", errors="replace") == content:
                return "unchanged"
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "written"


def write_movies(
    config: AppConfig,
    client: XtreamClient,
    movies: list[MovieItem],
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    if config.output.clean:
        ensure_empty_dir(config.output.movies_dir, config.output.dry_run)
    manifest = load_manifest(config)
    old_movies = manifest.get("movies") if isinstance(manifest.get("movies"), dict) else {}
    current_movies: dict[str, dict[str, Any]] = {}
    current_paths: set[Path] = set()
    written_paths: set[Path] = set()
    written = skipped_duplicate = file_written = file_unchanged = 0
    total = len(movies)
    last_progress = 0
    for index, item in enumerate(movies, start=1):
        folder = config.output.movies_dir / folder_name(
            item.name,
            item.year,
            item.tmdb_id,
            config.output.append_tmdb_id,
        )
        title = strip_redundant_year(clean_title(item.name), item.year)
        filename = safe_filename(f"{title} ({item.year})" if item.year else title)
        strm_path = folder / f"{filename}.strm"
        nfo_path = folder / f"{filename}.nfo"
        paths = [str(strm_path)]
        if config.output.generate_nfo:
            paths.append(str(nfo_path))
        key = movie_key(item)
        current_movies[key] = {
            "name": item.name,
            "stream_id": item.stream_id,
            "url": item.url or client.movie_url(item),
            "paths": paths,
        }
        current_paths.update(Path(path) for path in paths)
        if strm_path in written_paths:
            skipped_duplicate += 1
            if progress and _should_report_count(index, total):
                last_progress = index
                progress(index, total)
            continue

        strm_status = write_text(
            strm_path,
            item.url or client.movie_url(item),
            config.output.dry_run,
            skip_unchanged=config.output.incremental,
        )
        file_written += int(strm_status == "written")
        file_unchanged += int(strm_status == "unchanged")
        if config.output.generate_nfo:
            nfo_status = write_text(
                nfo_path,
                movie_nfo(item),
                config.output.dry_run,
                skip_unchanged=config.output.incremental,
            )
            file_written += int(nfo_status == "written")
            file_unchanged += int(nfo_status == "unchanged")
        written_paths.add(strm_path)
        written += 1
        if progress and _should_report_count(index, total):
            last_progress = index
            progress(index, total)
    if progress and last_progress != total:
        progress(total, total)

    removed = cleanup_stale_entries(
        old_movies,
        set(current_movies),
        current_paths,
        [config.output.movies_dir],
        config.output.dry_run,
    ) if config.output.cleanup_missing else {"files": 0, "dirs": 0}
    manifest["movies"] = current_movies
    save_manifest(config, manifest)
    return {
        "movies_written": written,
        "movie_duplicates_skipped": skipped_duplicate,
        "movie_files_written": file_written,
        "movie_files_unchanged": file_unchanged,
        "movie_files_removed": removed["files"],
        "movie_dirs_removed": removed["dirs"],
    }


def write_series(
    config: AppConfig,
    episodes: list[EpisodeItem],
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    if config.output.clean:
        ensure_empty_dir(config.output.series_dir, config.output.dry_run)
    manifest = load_manifest(config)
    old_series = manifest.get("series") if isinstance(manifest.get("series"), dict) else {}
    old_episodes = manifest.get("episodes") if isinstance(manifest.get("episodes"), dict) else {}
    current_series: dict[str, dict[str, Any]] = {}
    current_episodes: dict[str, dict[str, Any]] = {}
    current_paths: set[Path] = set()
    grouped: dict[str, list[EpisodeItem]] = defaultdict(list)
    for ep in episodes:
        grouped[ep.series.series_id].append(ep)

    series_written = episode_written = duplicate_paths = file_written = file_unchanged = 0
    processed = 0
    total = len(episodes)
    last_progress = 0
    written_paths: set[Path] = set()
    for eps in grouped.values():
        series = eps[0].series
        series_folder = config.output.series_dir / folder_name(
            series.name,
            series.year,
            series.tmdb_id,
            config.output.append_tmdb_id,
        )
        series_paths: list[str] = []
        if config.output.generate_nfo:
            tvshow_path = series_folder / "tvshow.nfo"
            status = write_text(
                tvshow_path,
                series_nfo(series),
                config.output.dry_run,
                skip_unchanged=config.output.incremental,
            )
            file_written += int(status == "written")
            file_unchanged += int(status == "unchanged")
            series_paths.append(str(tvshow_path))
            current_paths.add(tvshow_path)
        current_series[series_key(series)] = {
            "name": series.name,
            "series_id": series.series_id,
            "paths": series_paths,
        }
        wrote_any = False
        for ep in sorted(eps, key=lambda item: (item.season, item.episode, item.stream_id)):
            processed += 1
            title = safe_filename(f"{clean_title(series.name)} - S{ep.season:02d}E{ep.episode:02d} - {ep.title}")
            path = series_folder / f"Season {ep.season:02d}" / f"{title}.strm"
            nfo_path = path.with_suffix(".nfo")
            paths = [str(path)]
            if config.output.generate_nfo:
                paths.append(str(nfo_path))
            current_episodes[episode_key(ep)] = {
                "series_id": series.series_id,
                "stream_id": ep.stream_id,
                "season": ep.season,
                "episode": ep.episode,
                "url": ep.url,
                "paths": paths,
            }
            current_paths.update(Path(item) for item in paths)
            if path in written_paths:
                duplicate_paths += 1
                if progress and _should_report_count(processed, total):
                    last_progress = processed
                    progress(processed, total)
                continue
            status = write_text(path, ep.url, config.output.dry_run, skip_unchanged=config.output.incremental)
            file_written += int(status == "written")
            file_unchanged += int(status == "unchanged")
            if config.output.generate_nfo:
                nfo_status = write_text(
                    nfo_path,
                    episode_nfo(ep),
                    config.output.dry_run,
                    skip_unchanged=config.output.incremental,
                )
                file_written += int(nfo_status == "written")
                file_unchanged += int(nfo_status == "unchanged")
            written_paths.add(path)
            episode_written += 1
            wrote_any = True
            if progress and _should_report_count(processed, total):
                last_progress = processed
                progress(processed, total)
        if wrote_any:
            series_written += 1
    if progress and last_progress != total:
        progress(total, total)

    if config.output.cleanup_missing:
        removed_episodes = cleanup_stale_entries(
            old_episodes,
            set(current_episodes),
            current_paths,
            [config.output.series_dir],
            config.output.dry_run,
        )
        removed_series = cleanup_stale_entries(
            old_series,
            set(current_series),
            current_paths,
            [config.output.series_dir],
            config.output.dry_run,
        )
    else:
        removed_episodes = {"files": 0, "dirs": 0}
        removed_series = {"files": 0, "dirs": 0}
    manifest["series"] = current_series
    manifest["episodes"] = current_episodes
    save_manifest(config, manifest)
    return {
        "series_written": series_written,
        "episodes_written": episode_written,
        "episode_duplicates_skipped": duplicate_paths,
        "series_files_written": file_written,
        "series_files_unchanged": file_unchanged,
        "series_files_removed": removed_episodes["files"] + removed_series["files"],
        "series_dirs_removed": removed_episodes["dirs"] + removed_series["dirs"],
    }


def movie_key(item: MovieItem) -> str:
    return f"movie:{item.stream_id or item.url or item.name}"


def series_key(item: SeriesItem) -> str:
    return f"series:{item.series_id or item.name}"


def episode_key(item: EpisodeItem) -> str:
    return f"episode:{item.series.series_id}:{item.season}:{item.episode}:{item.stream_id}"


def load_manifest(config: AppConfig) -> dict[str, Any]:
    path = config.output.manifest_file
    if not path or config.output.clean:
        return {"version": 1, "movies": {}, "series": {}, "episodes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("movies", {})
    data.setdefault("series", {})
    data.setdefault("episodes", {})
    return data


def save_manifest(config: AppConfig, manifest: dict[str, Any]) -> None:
    path = config.output.manifest_file
    if not path or config.output.dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def cleanup_stale_entries(
    old_entries: dict[str, Any],
    current_keys: set[str],
    current_paths: set[Path],
    roots: list[Path],
    dry_run: bool,
) -> dict[str, int]:
    removed_files = removed_dirs = 0
    root_paths = [root.resolve(strict=False) for root in roots]
    active_paths = {path.resolve(strict=False) for path in current_paths}
    for key, entry in old_entries.items():
        if key in current_keys or not isinstance(entry, dict):
            continue
        for raw_path in entry.get("paths") or []:
            path = Path(str(raw_path))
            resolved = path.resolve(strict=False)
            if resolved in active_paths or not path_is_under_any(resolved, root_paths):
                continue
            if path.suffix not in {".strm", ".nfo"}:
                continue
            if not dry_run and path.exists() and path.is_file():
                path.unlink()
                removed_files += 1
                removed_dirs += prune_empty_dirs(path.parent, root_paths)
            elif dry_run:
                removed_files += 1
    return {"files": removed_files, "dirs": removed_dirs}


def prune_empty_dirs(start: Path, roots: list[Path]) -> int:
    removed = 0
    current = start.resolve(strict=False)
    while path_is_under_any(current, roots) and current not in roots:
        try:
            current.rmdir()
        except OSError:
            break
        removed += 1
        current = current.parent
    return removed


def path_is_under_any(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _should_report_count(current: int, total: int) -> bool:
    if total <= 0 or current >= total:
        return True
    step = max(1, total // 100)
    return current % step == 0
