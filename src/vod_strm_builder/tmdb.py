from __future__ import annotations

import json
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import requests

from .models import AppConfig, MovieItem, SeriesItem, TmdbConfig
from .utils import clean_title, normalize_name, strip_redundant_year


class TmdbClient:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, config: TmdbConfig, timeout: int = 20) -> None:
        if not config.api_key:
            raise ValueError(f"TMDB is enabled but no API key was provided via {config.api_key_env}.")
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self._cache: dict[str, Any] = {}
        if config.cache_file and config.cache_file.exists():
            try:
                self._cache = json.loads(config.cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._cache = {}

    def save_cache(self) -> None:
        if not self.config.cache_file:
            return
        self.config.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.cache_file.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def resolve_movie(self, item: MovieItem) -> tuple[MovieItem, bool]:
        if self.config.lookup_missing_only and item.tmdb_id:
            return item, False
        result = self._resolve(
            kind="movie",
            name=item.name,
            year=item.year,
            imdb_id=item.imdb_id,
        )
        if not result:
            return item, False
        return replace(item, tmdb_id=str(result["id"])), True

    def resolve_series(self, item: SeriesItem) -> tuple[SeriesItem, bool]:
        if self.config.lookup_missing_only and item.tmdb_id:
            return item, False
        result = self._resolve(
            kind="tv",
            name=item.name,
            year=item.year,
            imdb_id=item.imdb_id,
        )
        if not result:
            return item, False
        return replace(item, tmdb_id=str(result["id"])), True

    def _resolve(self, kind: str, name: str, year: int | None, imdb_id: str | None) -> dict[str, Any] | None:
        clean = strip_redundant_year(clean_title(name), year)
        cache_key = f"{kind}|{normalize_name(clean)}|{year or ''}|{imdb_id or ''}"
        if cache_key in self._cache:
            return self._cache[cache_key] or None

        result = self._find_by_imdb(kind, imdb_id) if imdb_id else None
        if not result:
            result = self._search(kind, clean, year)
        self._cache[cache_key] = result or None
        return result

    def _find_by_imdb(self, kind: str, imdb_id: str | None) -> dict[str, Any] | None:
        if not imdb_id:
            return None
        payload = self._get(
            f"/find/{imdb_id}",
            {"external_source": "imdb_id"},
        )
        key = "movie_results" if kind == "movie" else "tv_results"
        rows = payload.get(key) or []
        return rows[0] if rows else None

    def _search(self, kind: str, title: str, year: int | None) -> dict[str, Any] | None:
        endpoint = "/search/movie" if kind == "movie" else "/search/tv"
        params: dict[str, Any] = {
            "query": title,
            "include_adult": "false",
            "language": self.config.language,
        }
        if self.config.region:
            params["region"] = self.config.region
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        payload = self._get(endpoint, params)
        result = choose_best_result(kind, title, year, payload.get("results") or [])
        if result and result["score"] >= self.config.min_score:
            return result["row"]
        return None

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.BASE_URL}{endpoint}",
                params={"api_key": self.config.api_key, **params},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(f"TMDB request failed for endpoint {endpoint}") from None
        return response.json()


def choose_best_result(kind: str, title: str, year: int | None, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    target = normalize_name(title)
    for row in rows:
        candidate_title = row.get("title") if kind == "movie" else row.get("name")
        if not candidate_title:
            continue
        score = SequenceMatcher(None, target, normalize_name(str(candidate_title))).ratio()
        row_year = _year_from_row(kind, row)
        if year and row_year:
            if row_year == year:
                score += 0.25
            elif abs(row_year - year) == 1:
                score += 0.08
            else:
                score -= 0.18
        if row.get("popularity"):
            score += min(float(row["popularity"]), 100.0) / 1000.0
        item = {"row": row, "score": score}
        if not best or item["score"] > best["score"]:
            best = item
    return best


def enrich_with_tmdb(
    config: AppConfig,
    movies: list[MovieItem],
    series: list[SeriesItem],
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[MovieItem], list[SeriesItem], dict[str, int]]:
    if not config.tmdb.enabled:
        if progress:
            progress(0, 0)
        return movies, series, {"tmdb_enabled": 0}
    client = TmdbClient(config.tmdb)
    stats = {
        "tmdb_enabled": 1,
        "movies_checked": 0,
        "movies_resolved": 0,
        "series_checked": 0,
        "series_resolved": 0,
        "tmdb_errors": 0,
    }
    enriched_movies: list[MovieItem] = []
    stop_lookup = False
    total = len(movies) + len(series)
    checked = 0
    last_progress = 0
    for index, item in enumerate(movies):
        if stop_lookup:
            enriched_movies.extend(movies[index:])
            break
        checked += 1
        should_check = not (config.tmdb.lookup_missing_only and item.tmdb_id)
        if should_check:
            stats["movies_checked"] += 1
        try:
            resolved, changed = client.resolve_movie(item)
        except RuntimeError:
            stats["tmdb_errors"] += 1
            if config.tmdb.fail_on_error:
                raise
            enriched_movies.append(item)
            stop_lookup = True
            continue
        if changed:
            stats["movies_resolved"] += 1
        enriched_movies.append(resolved)
        if progress and _should_report_count(checked, total):
            last_progress = checked
            progress(checked, total)

    enriched_series: list[SeriesItem] = []
    for index, item in enumerate(series):
        if stop_lookup:
            enriched_series.extend(series[index:])
            break
        checked += 1
        should_check = not (config.tmdb.lookup_missing_only and item.tmdb_id)
        if should_check:
            stats["series_checked"] += 1
        try:
            resolved, changed = client.resolve_series(item)
        except RuntimeError:
            stats["tmdb_errors"] += 1
            if config.tmdb.fail_on_error:
                raise
            enriched_series.append(item)
            stop_lookup = True
            continue
        if changed:
            stats["series_resolved"] += 1
        enriched_series.append(resolved)
        if progress and _should_report_count(checked, total):
            last_progress = checked
            progress(checked, total)

    client.save_cache()
    if stop_lookup:
        stats["tmdb_lookup_stopped_after_error"] = 1
    if progress and last_progress != total:
        progress(total, total)
    return enriched_movies, enriched_series, stats


def _year_from_row(kind: str, row: dict[str, Any]) -> int | None:
    value = row.get("release_date") if kind == "movie" else row.get("first_air_date")
    if not value or len(str(value)) < 4:
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None


def _should_report_count(current: int, total: int) -> bool:
    if total <= 0 or current >= total:
        return True
    step = max(1, total // 100)
    return current % step == 0
