from vod_strm_builder.models import ProviderConfig, SeriesItem
from vod_strm_builder.xtream import XtreamClient


def test_series_episodes_builds_api_episode_urls():
    provider = ProviderConfig(
        server_url="http://provider.example.com",
        username="user",
        password="pass",
    )
    client = XtreamClient(provider)
    client.player_api = lambda action, **params: {
        "episodes": {
            "1": [
                {
                    "id": "123",
                    "episode_num": 2,
                    "container_extension": "mkv",
                    "title": "Pilot",
                    "info": {"movie_image": "http://image.example/poster.jpg"},
                }
            ]
        }
    }
    series = SeriesItem("Example Show", "42", "7", 2024, None, None, None, None, None, None)

    episodes = client.series_episodes(series)

    assert len(episodes) == 1
    assert episodes[0].season == 1
    assert episodes[0].episode == 2
    assert episodes[0].url == "http://provider.example.com/series/user/pass/123.mkv"
    assert episodes[0].logo == "http://image.example/poster.jpg"


def test_xtream_items_keep_secondary_category_ids():
    provider = ProviderConfig(
        server_url="http://provider.example.com",
        username="user",
        password="pass",
    )
    client = XtreamClient(provider)
    client.player_api = lambda action, **params: [
        {
            "name": "Movie",
            "stream_id": "1",
            "category_id": "10",
            "category_ids": [10, "11"],
            "container_extension": "mp4",
        }
    ]

    movies = client.movies()

    assert movies[0].category_id == "10"
    assert movies[0].category_ids == ("10", "11")


def test_xtream_player_api_tries_line_fallback(monkeypatch):
    provider = ProviderConfig(
        server_url="http://vpn.example.com",
        username="user",
        password="pass",
    )
    client = XtreamClient(provider)
    called_urls = []

    class Response:
        def __init__(self, ok):
            self.ok = ok
            self.status_code = 200 if ok else 403

        def raise_for_status(self):
            if not self.ok:
                import requests

                raise requests.HTTPError("403")

        def json(self):
            return [{"category_id": "1", "category_name": "Fallback"}]

    def fake_get(url, **kwargs):
        called_urls.append(url)
        return Response(ok="line.example.com" in url)

    monkeypatch.setattr(client.session, "get", fake_get)

    rows = client.player_api("get_live_categories")

    assert rows[0]["category_name"] == "Fallback"
    assert called_urls == [
        "http://vpn.example.com/player_api.php",
        "http://line.example.com/player_api.php",
    ]
