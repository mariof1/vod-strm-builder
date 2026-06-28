from pathlib import Path

import requests

from vod_strm_builder.webapp import build_config, describe_playlist_fetch_error, job_environment


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
