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
