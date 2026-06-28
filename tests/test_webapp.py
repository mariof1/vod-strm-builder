from pathlib import Path

from vod_strm_builder.webapp import build_config, job_environment


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
