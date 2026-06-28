import json
import os

from apps.vod.models import M3UVODCategoryRelation


ACCOUNT_NAME = os.environ.get("DISPATCHARR_ACCOUNT", "trex")

relations = (
    M3UVODCategoryRelation.objects.filter(
        m3u_account__name=ACCOUNT_NAME,
        m3u_account__is_active=True,
        enabled=True,
    )
    .select_related("category", "m3u_account")
    .order_by("category__category_type", "category__name")
)

movie_groups = []
series_groups = []
movie_category_ids = []
series_category_ids = []

for rel in relations:
    category = rel.category
    props = rel.custom_properties or {}
    provider_id = props.get("provider_category_id") or props.get("category_id") or props.get("xc_id")
    if category.category_type == "movie":
        movie_groups.append(category.name)
        if provider_id:
            movie_category_ids.append(str(provider_id))
    elif category.category_type == "series":
        series_groups.append(category.name)
        if provider_id:
            series_category_ids.append(str(provider_id))

print(
    json.dumps(
        {
            "account": ACCOUNT_NAME,
            "movie_groups": movie_groups,
            "series_groups": series_groups,
            "movie_category_ids": sorted(set(movie_category_ids), key=str),
            "series_category_ids": sorted(set(series_category_ids), key=str),
        },
        indent=2,
        ensure_ascii=False,
    )
)
