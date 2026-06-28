from __future__ import annotations

import json
from pathlib import Path

from .models import MovieItem, SeriesItem


def load_catalog(path: Path) -> tuple[list[MovieItem], list[SeriesItem], dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    movies = [
        MovieItem(
            name=str(item["name"]),
            stream_id=str(item["stream_id"]),
            category_id=str(item.get("category_id") or item.get("category_name") or ""),
            extension=str(item.get("extension") or "mp4").lstrip("."),
            year=item.get("year"),
            tmdb_id=_clean_id(item.get("tmdb_id")),
            imdb_id=_clean_id(item.get("imdb_id")),
            plot=item.get("plot"),
            genre=item.get("genre"),
            rating=item.get("rating"),
            cover=item.get("cover"),
        )
        for item in raw.get("movies", [])
        if item.get("name") and item.get("stream_id")
    ]
    series = [
        SeriesItem(
            name=str(item["name"]),
            series_id=str(item["series_id"]),
            category_id=str(item.get("category_id") or item.get("category_name") or ""),
            year=item.get("year"),
            tmdb_id=_clean_id(item.get("tmdb_id")),
            imdb_id=_clean_id(item.get("imdb_id")),
            plot=item.get("plot"),
            genre=item.get("genre"),
            rating=item.get("rating"),
            cover=item.get("cover"),
        )
        for item in raw.get("series", [])
        if item.get("name") and item.get("series_id")
    ]
    return movies, series, raw.get("metadata", {})


def _clean_id(value: object) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    text = str(value).strip()
    return text if text and text != "0" else None
