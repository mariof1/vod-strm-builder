import json
import os

from apps.vod.models import M3UMovieRelation, M3USeriesRelation, M3UVODCategoryRelation


ACCOUNT_NAME = os.environ.get("DISPATCHARR_ACCOUNT", "trex")

enabled_relations = (
    M3UVODCategoryRelation.objects.filter(
        m3u_account__name=ACCOUNT_NAME,
        m3u_account__is_active=True,
        enabled=True,
    )
    .select_related("category", "m3u_account")
    .order_by("category__category_type", "category__name")
)

enabled_movie_category_ids = {
    rel.category_id for rel in enabled_relations if rel.category.category_type == "movie"
}
enabled_series_category_ids = {
    rel.category_id for rel in enabled_relations if rel.category.category_type == "series"
}
movie_groups = [
    rel.category.name for rel in enabled_relations if rel.category.category_type == "movie"
]
series_groups = [
    rel.category.name for rel in enabled_relations if rel.category.category_type == "series"
]

movies = []
seen_movies = set()
movie_relations = (
    M3UMovieRelation.objects.filter(
        m3u_account__name=ACCOUNT_NAME,
        m3u_account__is_active=True,
        category_id__in=enabled_movie_category_ids,
    )
    .select_related("movie", "movie__logo", "category")
    .order_by("movie__name", "id")
)
for rel in movie_relations:
    movie = rel.movie
    if movie.id in seen_movies:
        continue
    seen_movies.add(movie.id)
    movies.append(
        {
            "name": movie.name,
            "stream_id": rel.stream_id,
            "extension": rel.container_extension or "mp4",
            "category_name": rel.category.name if rel.category else "",
            "year": movie.year,
            "tmdb_id": movie.tmdb_id,
            "imdb_id": movie.imdb_id,
            "plot": movie.description,
            "genre": movie.genre,
            "rating": movie.rating,
            "cover": movie.logo.url if movie.logo else None,
        }
    )

series = []
seen_series = set()
series_relations = (
    M3USeriesRelation.objects.filter(
        m3u_account__name=ACCOUNT_NAME,
        m3u_account__is_active=True,
        category_id__in=enabled_series_category_ids,
    )
    .select_related("series", "series__logo", "category")
    .order_by("series__name", "id")
)
for rel in series_relations:
    show = rel.series
    if show.id in seen_series:
        continue
    seen_series.add(show.id)
    series.append(
        {
            "name": show.name,
            "series_id": show.id,
            "external_series_id": rel.external_series_id,
            "category_name": rel.category.name if rel.category else "",
            "year": show.year,
            "tmdb_id": show.tmdb_id,
            "imdb_id": show.imdb_id,
            "plot": show.description,
            "genre": show.genre,
            "rating": show.rating,
            "cover": show.logo.url if show.logo else None,
        }
    )

print(
    json.dumps(
        {
            "metadata": {
                "account": ACCOUNT_NAME,
                "movie_groups": movie_groups,
                "series_groups": series_groups,
                "movie_count": len(movies),
                "series_count": len(series),
            },
            "movies": movies,
            "series": series,
        },
        indent=2,
        ensure_ascii=False,
    )
)

