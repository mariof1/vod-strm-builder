from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

import requests

from .models import EpisodeItem, SeriesItem
from .utils import normalize_name, qualityless_name

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
URL_RE = re.compile(r"/series/[^/]+/[^/]+/(?P<stream>[^/.?#]+)\.(?P<ext>[^/?#]+)")
EPISODE_PATTERNS = (
    re.compile(r"^(?P<base>.+?)\s+[Ss](?P<s>\d{1,2})\s*[ ._-]*\s*[Ee](?P<e>\d{1,4})(?P<tail>.*)$"),
    re.compile(r"^(?P<base>.+?)\s+(?P<s>\d{1,2})x(?P<e>\d{1,4})(?P<tail>.*)$", re.IGNORECASE),
)


@dataclass
class M3UGroupSummary:
    name: str
    movie_count: int = 0
    series_count: int = 0
    live_count: int = 0
    samples: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.movie_count + self.series_count + self.live_count

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "movie_count": self.movie_count,
            "series_count": self.series_count,
            "live_count": self.live_count,
            "total": self.total,
            "samples": sorted(self.samples)[:3],
        }


class SeriesParseStats(NamedTuple):
    seen_urls: int
    parsed_titles: int
    emitted: int
    skipped_group: int
    unmapped: int
    ambiguous: int


def parse_series_episodes(
    m3u_source: str,
    selected_series: Iterable[SeriesItem],
    selected_groups: set[str],
    user_agent: str,
    require_selected_group: bool,
    quality_words: tuple[str, ...],
) -> tuple[list[EpisodeItem], SeriesParseStats]:
    exact: dict[str, list[SeriesItem]] = {}
    fallback: dict[str, list[SeriesItem]] = {}
    for item in selected_series:
        exact.setdefault(normalize_name(item.name), []).append(item)
        fallback.setdefault(qualityless_name(item.name, quality_words), []).append(item)

    episodes: list[EpisodeItem] = []
    seen_stream_ids: set[str] = set()
    stats = {
        "seen_urls": 0,
        "parsed_titles": 0,
        "emitted": 0,
        "skipped_group": 0,
        "unmapped": 0,
        "ambiguous": 0,
    }

    for line_iter in (_iter_http_lines(m3u_source, user_agent) if m3u_source.startswith(("http://", "https://")) else _iter_file_lines(m3u_source),):
        extinf: str | None = None
        for line in line_iter:
            extinf = _consume_series_line(
                line,
                extinf,
                selected_groups,
                require_selected_group,
                exact,
                fallback,
                quality_words,
                seen_stream_ids,
                episodes,
                stats,
            )

    return episodes, SeriesParseStats(**stats)


def scan_m3u_groups(lines: Iterable[str]) -> list[M3UGroupSummary]:
    groups: dict[str, M3UGroupSummary] = {}
    extinf: str | None = None
    for raw_line in lines:
        line = (raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            extinf = line
            continue
        if line.startswith("#") or not extinf:
            continue

        attrs = dict(ATTR_RE.findall(extinf))
        group_name = (attrs.get("group-title") or "Ungrouped").strip() or "Ungrouped"
        title = (attrs.get("tvg-name") or extinf.split(",", 1)[-1] or line).strip()
        group = groups.setdefault(group_name, M3UGroupSummary(name=group_name))
        if "/movie/" in line.lower():
            group.movie_count += 1
        elif "/series/" in line.lower():
            group.series_count += 1
        else:
            group.live_count += 1
        if title and len(group.samples) < 8:
            group.samples.add(title)
        extinf = None

    return sorted(groups.values(), key=lambda group: group.name.lower())


def _parse_episode_title(title: str) -> tuple[str, int, int, str] | None:
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(title or "")
        if not match:
            continue
        tail = (match.group("tail") or "").strip(" -._")
        return (
            match.group("base").strip(),
            int(match.group("s")),
            int(match.group("e")),
            tail,
        )
    return None


def _iter_http_lines(url: str, user_agent: str):
    with requests.get(url, stream=True, timeout=(20, 180), headers={"User-Agent": user_agent}) as response:
        response.raise_for_status()
        yield from (line or "" for line in response.iter_lines(decode_unicode=True))


def _iter_file_lines(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line.rstrip("\n")


def _consume_series_line(
    line: str,
    extinf: str | None,
    selected_groups: set[str],
    require_selected_group: bool,
    exact: dict[str, list[SeriesItem]],
    fallback: dict[str, list[SeriesItem]],
    quality_words: tuple[str, ...],
    seen_stream_ids: set[str],
    episodes: list[EpisodeItem],
    stats: dict[str, int],
) -> str | None:
    if line.startswith("#EXTINF"):
        return line
    if "/series/" not in line or not extinf:
        return extinf
    stats["seen_urls"] += 1
    attrs = dict(ATTR_RE.findall(extinf))
    group_title = attrs.get("group-title") or ""
    if require_selected_group and selected_groups and group_title not in selected_groups:
        stats["skipped_group"] += 1
        return extinf
    url_match = URL_RE.search(line)
    if not url_match:
        return extinf
    title = attrs.get("tvg-name") or extinf.split(",", 1)[-1]
    parsed = _parse_episode_title(title)
    if not parsed:
        return extinf
    stats["parsed_titles"] += 1
    base_name, season, episode_num, episode_title = parsed
    matches = exact.get(normalize_name(base_name), [])
    if len(matches) != 1:
        matches = fallback.get(qualityless_name(base_name, quality_words), [])
    if len(matches) != 1:
        if len(matches) > 1:
            stats["ambiguous"] += 1
        else:
            stats["unmapped"] += 1
        return extinf
    stream_id = url_match.group("stream")
    if stream_id in seen_stream_ids:
        return extinf
    seen_stream_ids.add(stream_id)
    episodes.append(
        EpisodeItem(
            series=matches[0],
            season=season,
            episode=episode_num,
            title=episode_title or f"Episode {episode_num:02d}",
            stream_id=stream_id,
            extension=url_match.group("ext").split("?", 1)[0].lower(),
            url=line,
            logo=attrs.get("tvg-logo") or None,
        )
    )
    stats["emitted"] += 1
    return extinf
