from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = "TiviMate/5.1.6 (Android 12)"


@dataclass(frozen=True)
class ProviderConfig:
    server_url: str
    username: str
    password: str
    m3u_url: str | None = None
    m3u_file: Path | None = None
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class OutputConfig:
    movies_dir: Path
    series_dir: Path
    append_tmdb_id: bool = True
    generate_nfo: bool = True
    clean: bool = False
    dry_run: bool = False
    incremental: bool = True
    cleanup_missing: bool = False
    manifest_file: Path | None = None


@dataclass(frozen=True)
class FilterConfig:
    movie_groups: set[str] = field(default_factory=set)
    series_groups: set[str] = field(default_factory=set)
    movie_category_ids: set[str] = field(default_factory=set)
    series_category_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class SeriesConfig:
    source: str = "m3u"
    require_selected_m3u_group: bool = True
    quality_words: tuple[str, ...] = ("4k", "uhd", "fhd", "hd")


@dataclass(frozen=True)
class TmdbConfig:
    enabled: bool = False
    api_key: str | None = None
    api_key_env: str = "TMDB_API_KEY"
    language: str = "en-US"
    region: str | None = None
    cache_file: Path | None = None
    lookup_missing_only: bool = True
    min_score: float = 0.58
    fail_on_error: bool = False


@dataclass(frozen=True)
class JellyfinConfig:
    enabled: bool = False
    server_url: str | None = None
    api_key: str | None = None
    api_key_env: str = "JELLYFIN_API_KEY"
    scan_on_complete: bool = True
    library_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderConfig
    output: OutputConfig
    filters: FilterConfig
    series: SeriesConfig = field(default_factory=SeriesConfig)
    catalog_file: Path | None = None
    tmdb: TmdbConfig = field(default_factory=TmdbConfig)
    jellyfin: JellyfinConfig = field(default_factory=JellyfinConfig)


@dataclass(frozen=True)
class MovieItem:
    name: str
    stream_id: str
    category_id: str
    extension: str
    year: int | None
    tmdb_id: str | None
    imdb_id: str | None
    plot: str | None
    genre: str | None
    rating: str | None
    cover: str | None
    url: str | None = None
    category_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeriesItem:
    name: str
    series_id: str
    category_id: str
    year: int | None
    tmdb_id: str | None
    imdb_id: str | None
    plot: str | None
    genre: str | None
    rating: str | None
    cover: str | None
    category_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeItem:
    series: SeriesItem
    season: int
    episode: int
    title: str
    stream_id: str
    extension: str
    url: str
    logo: str | None = None
