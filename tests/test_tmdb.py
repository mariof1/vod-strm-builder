from vod_strm_builder.tmdb import choose_best_result, tmdb_auth


def test_choose_best_result_prefers_title_and_year_match():
    result = choose_best_result(
        "movie",
        "The Matrix",
        1999,
        [
            {"id": 999, "title": "The Matrix Resurrections", "release_date": "2021-12-16", "popularity": 100},
            {"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "popularity": 10},
        ],
    )
    assert result is not None
    assert result["row"]["id"] == 603


def test_tmdb_auth_accepts_v3_key_or_read_access_token():
    assert tmdb_auth("abc123") == ({"api_key": "abc123"}, {})
    assert tmdb_auth("Bearer token.value.parts") == ({}, {"Authorization": "Bearer token.value.parts"})
    assert tmdb_auth("eyJhbGciOiJIUzI1NiJ9.payload.sig") == (
        {},
        {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"},
    )
