from __future__ import annotations

from html import escape

from .models import EpisodeItem, MovieItem, SeriesItem
from .utils import clean_title, strip_redundant_year


def movie_nfo(item: MovieItem) -> str:
    title = strip_redundant_year(clean_title(item.name), item.year)
    parts = ["<movie>", f"  <title>{escape(title)}</title>"]
    if item.year:
        parts.append(f"  <year>{item.year}</year>")
    if item.tmdb_id:
        parts.append(f"  <tmdbid>{escape(item.tmdb_id)}</tmdbid>")
        parts.append(f'  <uniqueid type="tmdb" default="true">{escape(item.tmdb_id)}</uniqueid>')
    if item.genre:
        parts.append(f"  <genre>{escape(item.genre)}</genre>")
    if item.rating:
        parts.append(f"  <rating>{escape(item.rating)}</rating>")
    if item.plot:
        parts.append(f"  <plot>{escape(item.plot)}</plot>")
    parts.append("</movie>")
    return "\n".join(parts) + "\n"


def series_nfo(item: SeriesItem) -> str:
    title = strip_redundant_year(clean_title(item.name), item.year)
    parts = ["<tvshow>", f"  <title>{escape(title)}</title>"]
    if item.year:
        parts.append(f"  <year>{item.year}</year>")
    if item.tmdb_id:
        parts.append(f"  <tmdbid>{escape(item.tmdb_id)}</tmdbid>")
        parts.append(f'  <uniqueid type="tmdb" default="true">{escape(item.tmdb_id)}</uniqueid>')
    if item.genre:
        parts.append(f"  <genre>{escape(item.genre)}</genre>")
    if item.rating:
        parts.append(f"  <rating>{escape(item.rating)}</rating>")
    if item.plot:
        parts.append(f"  <plot>{escape(item.plot)}</plot>")
    parts.append("</tvshow>")
    return "\n".join(parts) + "\n"


def episode_nfo(item: EpisodeItem) -> str:
    parts = [
        "<episodedetails>",
        f"  <title>{escape(item.title)}</title>",
        f"  <season>{item.season}</season>",
        f"  <episode>{item.episode}</episode>",
    ]
    if item.logo:
        parts.append(f"  <thumb>{escape(item.logo)}</thumb>")
    parts.append("</episodedetails>")
    return "\n".join(parts) + "\n"

