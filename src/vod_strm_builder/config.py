from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, FilterConfig, OutputConfig, ProviderConfig, SeriesConfig


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _string_set(value: Any) -> set[str]:
    if not value:
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def load_group_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Group file must be a JSON object.")
    return data


def load_config(path: str) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    group_file = load_group_file(raw.get("selected_groups_file"))

    provider_raw = raw.get("provider") or {}
    output_raw = raw.get("output") or {}
    filter_raw = raw.get("filters") or {}
    series_raw = raw.get("series") or {}

    username = provider_raw.get("username") or _required_env(provider_raw.get("username_env", "XTREAM_USERNAME"))
    password = provider_raw.get("password") or _required_env(provider_raw.get("password_env", "XTREAM_PASSWORD"))
    m3u_url = provider_raw.get("m3u_url")
    if not m3u_url and provider_raw.get("m3u_url_env"):
        m3u_url = os.environ.get(provider_raw["m3u_url_env"])

    provider = ProviderConfig(
        server_url=str(provider_raw["server_url"]).rstrip("/"),
        username=str(username),
        password=str(password),
        m3u_url=m3u_url,
        m3u_file=Path(provider_raw["m3u_file"]).expanduser() if provider_raw.get("m3u_file") else None,
        user_agent=str(provider_raw.get("user_agent") or "vod-strm-builder/0.1"),
    )

    output = OutputConfig(
        movies_dir=Path(output_raw["movies_dir"]).expanduser(),
        series_dir=Path(output_raw["series_dir"]).expanduser(),
        append_tmdb_id=bool(output_raw.get("append_tmdb_id", True)),
        generate_nfo=bool(output_raw.get("generate_nfo", True)),
        clean=bool(output_raw.get("clean", False)),
        dry_run=bool(output_raw.get("dry_run", False)),
    )

    filters = FilterConfig(
        movie_groups=_string_set(filter_raw.get("movie_groups")) | _string_set(group_file.get("movie_groups")),
        series_groups=_string_set(filter_raw.get("series_groups")) | _string_set(group_file.get("series_groups")),
        movie_category_ids=_string_set(filter_raw.get("movie_category_ids")) | _string_set(group_file.get("movie_category_ids")),
        series_category_ids=_string_set(filter_raw.get("series_category_ids")) | _string_set(group_file.get("series_category_ids")),
    )

    series = SeriesConfig(
        source=str(series_raw.get("source", "m3u")),
        require_selected_m3u_group=bool(series_raw.get("require_selected_m3u_group", True)),
        quality_words=tuple(series_raw.get("quality_words") or ("4k", "uhd", "fhd", "hd")),
    )

    return AppConfig(
        provider=provider,
        output=output,
        filters=filters,
        series=series,
        catalog_file=Path(raw["catalog_file"]).expanduser() if raw.get("catalog_file") else None,
    )
