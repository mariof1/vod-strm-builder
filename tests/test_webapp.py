from pathlib import Path

import requests

from vod_strm_builder.models import DEFAULT_USER_AGENT, MovieItem, SeriesItem
from vod_strm_builder.webapp import (
    AppState,
    build_config,
    create_app,
    describe_playlist_fetch_error,
    job_environment,
    selected_groups_by_provider,
    settings_providers,
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


def test_settings_api_persists_to_work_dir(tmp_path: Path):
    app = create_app(tmp_path)
    payload = {
        "settings": {
            "providers": [
                {
                    "id": "main",
                    "name": "Main",
                    "server_url": "http://provider.example.com",
                    "username": "user",
                    "password": "secret",
                }
            ],
            "movies_dir": "/media/movies",
        },
        "selected_groups": {"providers": {"main": {"movie_groups": ["Movies"]}}},
    }

    save_response = app.test_client().post("/api/settings", json=payload)
    load_response = app.test_client().get("/api/settings")

    assert save_response.status_code == 200
    assert load_response.get_json() == payload
    assert (tmp_path / "web-settings.json").exists()


def test_group_cache_api_persists_last_scan(tmp_path: Path):
    app = create_app(tmp_path)
    payload = {
        "providers": [
            {
                "id": "main",
                "name": "Main",
                "server_url": "http://provider.example.com",
                "username": "user",
                "password": "secret",
            }
        ],
        "active_provider_id": "main",
        "text": "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 group-title="Movies" tvg-name="Film",Film',
                "http://provider.example.com/movie/user/pass/1.mp4",
            ]
        ),
    }

    scan_response = app.test_client().post("/api/playlist/text", json=payload)
    reloaded_app = create_app(tmp_path)
    cache_response = reloaded_app.test_client().get("/api/groups")
    data = cache_response.get_json()

    assert scan_response.status_code == 200
    assert cache_response.status_code == 200
    assert data["groups"][0]["name"] == "Movies"
    assert data["groups"][0]["provider_id"] == "main"
    assert data["stats"]["movie_entries"] == 1
    assert (tmp_path / "web-groups.json").exists()


def test_group_cache_api_rebuilds_from_cached_playlist(tmp_path: Path):
    state = AppState(tmp_path)
    state.save_settings(
        {
            "settings": {
                "providers": [
                    {
                        "id": "main",
                        "name": "Main",
                        "server_url": "http://provider.example.com",
                        "username": "user",
                        "password": "secret",
                    }
                ]
            }
        }
    )
    state.write_and_scan_playlist(
        "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 group-title="Cached Movies" tvg-name="Film",Film',
                "http://provider.example.com/movie/user/pass/1.mp4",
            ]
        ),
        "main",
    )
    app = create_app(tmp_path)

    response = app.test_client().get("/api/groups")
    data = response.get_json()

    assert response.status_code == 200
    assert data["groups"][0]["name"] == "Cached Movies"
    assert data["groups"][0]["provider_id"] == "main"
    assert data["stats"]["movie_entries"] == 1
    assert (tmp_path / "web-groups.json").exists()


def test_multi_provider_selection_is_split_by_provider():
    providers = settings_providers(
        {
            "providers": [
                {"id": "one", "name": "One", "server_url": "http://one.example", "username": "u", "password": "p"},
                {"id": "two", "name": "Two", "server_url": "http://two.example", "username": "u", "password": "p"},
            ]
        }
    )

    selected = selected_groups_by_provider(
        {
            "providers": {
                "one": {"movie_groups": ["Movies"]},
                "two": {"series_groups": ["Series"]},
            }
        },
        providers,
    )

    assert selected["one"]["movie_groups"] == ["Movies"]
    assert selected["one"]["series_groups"] == []
    assert selected["two"]["movie_groups"] == []
    assert selected["two"]["series_groups"] == ["Series"]


def test_fetch_and_scan_playlist_handles_byte_lines(tmp_path: Path, monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=True):
            yield b'#EXTINF:-1 group-title="Movies" tvg-name="Film",Film'
            yield b"http://example.test/movie/user/pass/1.mp4"

    monkeypatch.setattr("vod_strm_builder.webapp.requests.get", lambda *args, **kwargs: FakeResponse())
    state = AppState(tmp_path)

    groups = state.fetch_and_scan_playlist("http://provider.example.com/get.php", DEFAULT_USER_AGENT)

    assert groups[0]["name"] == "Movies"
    assert groups[0]["movie_count"] == 1
    assert state.playlist_cache.read_text(encoding="utf-8").startswith("#EXTINF")


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
