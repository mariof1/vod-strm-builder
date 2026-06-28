from pathlib import Path

import requests

from vod_strm_builder.models import DEFAULT_USER_AGENT, MovieItem, SeriesItem
from vod_strm_builder.webapp import (
    build_config,
    create_app,
    describe_playlist_fetch_error,
    job_environment,
    xtream_group_summaries,
)


def test_build_config_uses_cached_playlist_and_env_secrets(tmp_path: Path):
    settings = {
        "server_url": "http://provider.example.com/",
        "username": "user",
        "password": "pass",
        "movies_dir": "/media/movies",
        "series_dir": "/media/tvshows",
        "append_tmdb": True,
        "generate_nfo": True,
        "clean_output": False,
        "dry_run": True,
        "require_selected_group": True,
        "quality_words": ["4k", "hd"],
        "tmdb_enabled": False,
        "tmdb_missing_only": True,
        "jellyfin_enabled": False,
        "jellyfin_scan": True,
    }
    config = build_config(settings, tmp_path / "selected-groups.json", tmp_path / "playlist.m3u")
    env = job_environment(settings)

    assert config["provider"]["server_url"] == "http://provider.example.com"
    assert config["provider"]["username_env"] == "XTREAM_USERNAME"
    assert config["provider"]["m3u_file"] == str(tmp_path / "playlist.m3u")
    assert config["output"]["movies_dir"] == "/media/movies"
    assert env["XTREAM_USERNAME"] == "user"
    assert env["XTREAM_PASSWORD"] == "pass"


def test_build_config_accepts_api_series_source(tmp_path: Path):
    settings = {
        "server_url": "http://provider.example.com/",
        "username": "user",
        "password": "pass",
        "movies_dir": "/media/movies",
        "series_dir": "/media/tvshows",
        "series_source": "api",
        "append_tmdb": True,
        "generate_nfo": True,
        "dry_run": True,
    }

    config = build_config(settings, tmp_path / "selected-groups.json", None)

    assert config["provider"]["user_agent"] == DEFAULT_USER_AGENT
    assert config["series"]["source"] == "api"


def test_describe_playlist_fetch_error_hides_url():
    response = requests.Response()
    response.status_code = 403
    response.reason = "Forbidden"
    response.url = "http://provider.example.com/get.php?username=user&password=secret"
    error = requests.HTTPError("403 Client Error", response=response)

    message = describe_playlist_fetch_error(error)

    assert message == "Playlist fetch failed for the configured provider: HTTP 403 Forbidden."
    assert "provider.example.com" not in message
    assert "secret" not in message


def test_fetch_playlist_bad_json_returns_json_error(tmp_path: Path):
    app = create_app(tmp_path)

    response = app.test_client().post("/api/playlist/fetch", data="{", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Provider URL, username, and password are required."}


def test_xtream_group_summaries_merge_movie_and_series_categories():
    movies = [
        MovieItem("Movie A", "1", "10", "mp4", None, None, None, None, None, None, None),
        MovieItem("Movie B", "2", "10", "mp4", None, None, None, None, None, None, None),
    ]
    series = [SeriesItem("Show A", "3", "20", None, None, None, None, None, None, None)]

    groups = xtream_group_summaries({"10": "Films"}, {"20": "Shows"}, movies, series)
    by_name = {group["name"]: group for group in groups}

    assert by_name["Films"]["movie_count"] == 2
    assert by_name["Films"]["series_count"] == 0
    assert by_name["Shows"]["movie_count"] == 0
    assert by_name["Shows"]["series_count"] == 1
