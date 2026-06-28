import json
from types import SimpleNamespace

import yaml

from vod_strm_builder.cli import existing_tmdb_stats, generate
from vod_strm_builder.m3u import _parse_episode_title, scan_m3u_groups
from vod_strm_builder.utils import clean_title, folder_name


def test_clean_title_strips_provider_prefix():
    assert clean_title("EN - The Matrix (1999)") == "The Matrix (1999)"


def test_folder_name_appends_tmdb_suffix():
    assert folder_name("EN - The Matrix (1999)", 1999, "603", True) == "The Matrix (1999) {tmdb-603}"


def test_parse_episode_title():
    parsed = _parse_episode_title("EN - The Handmaid's Tale (2017) S01 E03")
    assert parsed == ("EN - The Handmaid's Tale (2017)", 1, 3, "")


def test_existing_tmdb_stats_counts_provider_ids():
    movies = [SimpleNamespace(tmdb_id="603"), SimpleNamespace(tmdb_id=None)]
    series = [SimpleNamespace(tmdb_id="1399"), SimpleNamespace(tmdb_id=""), SimpleNamespace(tmdb_id=None)]
    assert existing_tmdb_stats(movies, series) == {
        "movies_with_provider_tmdb_id": 1,
        "series_with_provider_tmdb_id": 1,
    }


def test_scan_m3u_groups_counts_vod_types():
    groups = scan_m3u_groups(
        [
            '#EXTINF:-1 group-title="Movies" tvg-name="Film",Film',
            "http://example.test/movie/user/pass/1.mp4",
            '#EXTINF:-1 group-title="Series" tvg-name="Show S01 E01",Show S01 E01',
            "http://example.test/series/user/pass/2.mkv",
            '#EXTINF:-1 group-title="Live" tvg-name="News",News',
            "http://example.test/live/user/pass/3.ts",
        ]
    )
    by_name = {group.name: group for group in groups}
    assert by_name["Movies"].movie_count == 1
    assert by_name["Series"].series_count == 1
    assert by_name["Live"].live_count == 1


def test_generate_uses_cached_m3u_catalog_for_selected_groups(tmp_path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 group-title="|EN| Movies" tvg-name="EN - Test Movie (2025)" tvg-logo="movie.jpg",Test Movie',
                "http://cdn.example/movie/user/pass/101.mp4",
                '#EXTINF:-1 group-title="|EN| Other" tvg-name="EN - Other Movie (2025)",Other Movie',
                "http://cdn.example/movie/user/pass/999.mp4",
                '#EXTINF:-1 group-title="|EN| Series" tvg-name="EN - Test Show (2024) S01 E01" tvg-logo="show.jpg",Episode',
                "http://cdn.example/series/user/pass/201.mkv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    selected_groups = tmp_path / "selected-groups.json"
    selected_groups.write_text(
        json.dumps({"movie_groups": ["|EN| Movies"], "series_groups": ["|EN| Series"]}),
        encoding="utf-8",
    )
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "provider": {
                    "server_url": "http://127.0.0.1:9",
                    "username": "user",
                    "password": "pass",
                    "m3u_file": str(playlist),
                },
                "selected_groups_file": str(selected_groups),
                "output": {
                    "movies_dir": str(tmp_path / "movies"),
                    "series_dir": str(tmp_path / "tvshows"),
                    "append_tmdb_id": True,
                    "generate_nfo": False,
                    "clean": False,
                    "dry_run": True,
                },
                "series": {
                    "source": "m3u",
                    "require_selected_m3u_group": True,
                    "quality_words": ["4k", "uhd", "fhd", "hd"],
                },
                "tmdb": {"enabled": False},
                "jellyfin": {"enabled": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    progress_events = []
    summary = generate(str(config), progress=progress_events.append)

    assert summary["catalog_source"] == "m3u"
    assert summary["movies_selected"] == 1
    assert summary["series_selected"] == 1
    assert summary["movies_written"] == 1
    assert summary["episodes_written"] == 1
    assert "append_tmdb_id is enabled" in summary["warnings"][0]
    assert progress_events[-1]["label"] == "Provider complete"
    assert progress_events[-1]["percent"] == 100
    assert any(event["label"] == "Scanning playlist catalog" for event in progress_events)
    assert any(event["label"] == "Scanning series episodes" for event in progress_events)


def test_generate_skips_series_scan_when_no_series_selected(tmp_path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 group-title="Movies" tvg-name="Test Movie (2025)",Test Movie',
                "http://cdn.example/movie/user/pass/101.mp4",
                '#EXTINF:-1 group-title="Series" tvg-name="Test Show (2024) S01 E01",Episode',
                "http://cdn.example/series/user/pass/201.mkv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    selected_groups = tmp_path / "selected-groups.json"
    selected_groups.write_text(json.dumps({"movie_groups": ["Movies"], "series_groups": []}), encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "provider": {
                    "server_url": "http://127.0.0.1:9",
                    "username": "user",
                    "password": "pass",
                    "m3u_file": str(playlist),
                },
                "selected_groups_file": str(selected_groups),
                "output": {
                    "movies_dir": str(tmp_path / "movies"),
                    "series_dir": str(tmp_path / "tvshows"),
                    "append_tmdb_id": True,
                    "generate_nfo": False,
                    "clean": False,
                    "dry_run": True,
                },
                "series": {"source": "m3u", "require_selected_m3u_group": True},
                "tmdb": {"enabled": False},
                "jellyfin": {"enabled": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    progress_events = []
    summary = generate(str(config), progress=progress_events.append)

    assert summary["series_selected"] == 0
    assert summary["m3u_series_parse"]["seen_urls"] == 0
    assert summary["m3u_series_parse"]["unmapped"] == 0
    assert any(event["label"] == "No selected series to scan" for event in progress_events)
