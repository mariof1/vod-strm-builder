from vod_strm_builder.m3u import _parse_episode_title
from vod_strm_builder.utils import clean_title, folder_name


def test_clean_title_strips_provider_prefix():
    assert clean_title("EN - The Matrix (1999)") == "The Matrix (1999)"


def test_folder_name_appends_tmdb_suffix():
    assert folder_name("EN - The Matrix (1999)", 1999, "603", True) == "The Matrix (1999) {tmdb-603}"


def test_parse_episode_title():
    parsed = _parse_episode_title("EN - The Handmaid's Tale (2017) S01 E03")
    assert parsed == ("EN - The Handmaid's Tale (2017)", 1, 3, "")

