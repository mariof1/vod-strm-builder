from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import requests

from .models import EpisodeItem, MovieItem, ProviderConfig, SeriesItem
from .utils import clean_title, extract_year


class XtreamClient:
    def __init__(self, provider: ProviderConfig, timeout: int = 45) -> None:
        self.provider = provider
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": provider.user_agent})

    def player_api(self, action: str, **extra_params: object) -> Any:
        params = {
            "username": self.provider.username,
            "password": self.provider.password,
            "action": action,
        }
        params.update({key: value for key, value in extra_params.items() if value is not None})
        last_error = ""
        for server_url in self.player_api_server_urls():
            try:
                response = self.session.get(
                    f"{server_url}/player_api.php",
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    continue
                return response.json()
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                continue
            except ValueError:
                last_error = "non-JSON response"
                continue
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Xtream player_api action '{action}' failed for configured server{detail}") from None

    def player_api_server_urls(self) -> list[str]:
        candidates = [self.provider.server_url]
        if self.provider.m3u_url:
            parsed = urlparse(self.provider.m3u_url)
            if parsed.scheme and parsed.netloc:
                candidates.append(urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")))
        parsed = urlparse(self.provider.server_url)
        if parsed.scheme and parsed.netloc:
            host = parsed.netloc
            if host.startswith("vpn."):
                candidates.append(urlunparse((parsed.scheme, f"line.{host[4:]}", "", "", "", "")))
            elif host.startswith("line."):
                candidates.append(urlunparse((parsed.scheme, f"vpn.{host[5:]}", "", "", "", "")))
        return _dedupe_urls(candidates)

    def categories(self, kind: str) -> dict[str, str]:
        action = {
            "movie": "get_vod_categories",
            "series": "get_series_categories",
            "live": "get_live_categories",
        }[kind]
        rows = self.player_api(action)
        return {str(row.get("category_id")): str(row.get("category_name") or "") for row in rows or []}

    def live_streams(self) -> list[dict[str, object]]:
        rows = self.player_api("get_live_streams") or []
        streams: list[dict[str, object]] = []
        for row in rows:
            name = str(row.get("name") or row.get("title") or "").strip()
            stream_id = str(row.get("stream_id") or "")
            if not name or not stream_id:
                continue
            primary = str(row.get("category_id") or "")
            streams.append(
                {
                    "name": name,
                    "stream_id": stream_id,
                    "category_id": primary,
                    "category_ids": _category_ids(row, primary),
                }
            )
        return streams

    def movies(self) -> list[MovieItem]:
        rows = self.player_api("get_vod_streams") or []
        items: list[MovieItem] = []
        for row in rows:
            name = str(row.get("name") or row.get("title") or "").strip()
            stream_id = str(row.get("stream_id") or "")
            if not name or not stream_id:
                continue
            ext = str(row.get("container_extension") or "mp4").lstrip(".")
            tmdb_id = _clean_id(row.get("tmdb") or row.get("tmdb_id"))
            primary = str(row.get("category_id") or "")
            items.append(
                MovieItem(
                    name=name,
                    stream_id=stream_id,
                    category_id=primary,
                    extension=ext,
                    year=extract_year(name, row.get("releaseDate"), row.get("release_date"), row.get("year")),
                    tmdb_id=tmdb_id,
                    imdb_id=_clean_id(row.get("imdb") or row.get("imdb_id")),
                    plot=_string_or_none(row.get("plot")),
                    genre=_string_or_none(row.get("genre")),
                    rating=_string_or_none(row.get("rating")),
                    cover=_string_or_none(row.get("stream_icon") or row.get("cover")),
                    category_ids=_category_ids(row, primary),
                )
            )
        return items

    def series(self) -> list[SeriesItem]:
        rows = self.player_api("get_series") or []
        items: list[SeriesItem] = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            series_id = str(row.get("series_id") or "")
            if not name or not series_id:
                continue
            tmdb_id = _clean_id(row.get("tmdb") or row.get("tmdb_id"))
            primary = str(row.get("category_id") or "")
            items.append(
                SeriesItem(
                    name=name,
                    series_id=series_id,
                    category_id=primary,
                    year=extract_year(name, row.get("releaseDate"), row.get("release_date"), row.get("year")),
                    tmdb_id=tmdb_id,
                    imdb_id=_clean_id(row.get("imdb") or row.get("imdb_id")),
                    plot=_string_or_none(row.get("plot")),
                    genre=_string_or_none(row.get("genre")),
                    rating=_string_or_none(row.get("rating")),
                    cover=_string_or_none(row.get("cover")),
                    category_ids=_category_ids(row, primary),
                )
            )
        return items

    def movie_url(self, item: MovieItem) -> str:
        return (
            f"{self.provider.server_url}/movie/"
            f"{quote(self.provider.username)}/{quote(self.provider.password)}/"
            f"{quote(item.stream_id)}.{item.extension or 'mp4'}"
        )

    def series_episode_url(self, stream_id: str, extension: str) -> str:
        return (
            f"{self.provider.server_url}/series/"
            f"{quote(self.provider.username)}/{quote(self.provider.password)}/"
            f"{quote(stream_id)}.{extension or 'mp4'}"
        )

    def series_episodes(self, item: SeriesItem) -> list[EpisodeItem]:
        payload = self.player_api("get_series_info", series_id=item.series_id) or {}
        episodes = payload.get("episodes") if isinstance(payload, dict) else {}
        if not isinstance(episodes, dict):
            return []

        items: list[EpisodeItem] = []
        for season_key, rows in episodes.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                stream_id = str(row.get("id") or row.get("stream_id") or "").strip()
                if not stream_id:
                    continue
                season = _int_or_none(row.get("season") or season_key)
                episode = _int_or_none(row.get("episode_num") or row.get("episode") or row.get("num"))
                if season is None or episode is None:
                    continue
                title = str(row.get("title") or "").strip() or f"Episode {episode:02d}"
                extension = str(row.get("container_extension") or "mp4").lstrip(".")
                info = row.get("info") if isinstance(row.get("info"), dict) else {}
                logo = _string_or_none(info.get("movie_image") or row.get("cover")) if isinstance(info, dict) else None
                items.append(
                    EpisodeItem(
                        series=item,
                        season=season,
                        episode=episode,
                        title=title,
                        stream_id=stream_id,
                        extension=extension,
                        url=self.series_episode_url(stream_id, extension),
                        logo=logo,
                    )
                )
        return items

    def m3u_source(self) -> str:
        if self.provider.m3u_file:
            return str(self.provider.m3u_file)
        if self.provider.m3u_url:
            return self.provider.m3u_url
        return (
            f"{self.provider.server_url}/get.php?"
            f"username={quote(self.provider.username)}&password={quote(self.provider.password)}"
            "&type=m3u_plus&output=ts"
        )


def _clean_id(value: object) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    text = str(value).strip()
    return text if text and text != "0" else None


def _string_or_none(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return str(value)


def _category_ids(row: dict[str, Any], primary: str) -> tuple[str, ...]:
    values: list[str] = []
    raw = row.get("category_ids")
    if isinstance(raw, list):
        values.extend(str(item) for item in raw)
    elif isinstance(raw, str):
        cleaned = raw.strip().strip("[]")
        values.extend(part.strip().strip("'\"") for part in cleaned.replace("|", ",").split(","))
    elif raw not in (None, ""):
        values.append(str(raw))
    if primary:
        values.insert(0, primary)
    return tuple(dict.fromkeys(value for value in values if value))


def _dedupe_urls(urls: list[str]) -> list[str]:
    clean: list[str] = []
    for url in urls:
        value = str(url or "").rstrip("/")
        if value and value not in clean:
            clean.append(value)
    return clean


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
