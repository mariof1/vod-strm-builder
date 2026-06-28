# VOD STRM Builder

Generate a media-server-friendly VOD library directly from an Xtream/M3U provider.

It writes:

- movie `.strm` files and movie `.nfo`
- TV episode `.strm` files, `tvshow.nfo`, and episode `.nfo`
- folder names with Jellyfin/Plex-style TMDB suffixes, for example `The Matrix (1999) {tmdb-603}`

The important design choice is that series episodes are read from the provider's `m3u_plus` playlist instead of calling `player_api.php?action=get_series_info` for every series. That avoids slow or flaky per-series Xtream API scans.

## Docker Web App

The easiest way to run everything from the browser is the local Docker app:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

The web app can fetch or upload the playlist, select movie and series groups, write the generator config into the container work directory, run the Python generator, and show the job log.

Edit `docker-compose.yml` before first run so the container output paths point at your media folders:

```yaml
volumes:
  - ./work:/work
  - /mnt/nas/strm/movies:/media/movies
  - /mnt/nas/strm/tvshows:/media/tvshows
```

The frontend defaults to `/media/movies` and `/media/tvshows`, which are the in-container paths from the compose file.

### Public Image And Portainer

GitHub Actions builds and publishes the public Docker Hub image:

```text
mars148/vod-strm-builder:latest
```

For Portainer, use the public stack file:

```text
deploy/portainer-stack.yml
```

The stack publishes the app on host port `18080` and uses these default host paths:

```text
/opt/vod-strm-builder/work
/mnt/strm/movies
/mnt/strm/tvshows
```

The Portainer stack runs the container as `999:996`, matching the `/mnt/strm` NAS mount owner/group on the target Docker host.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Configure

Copy the example:

```bash
cp examples/config.example.yml config.yml
```

Set credentials in environment variables rather than in the config file:

```bash
export XTREAM_USERNAME="your_username"
export XTREAM_PASSWORD="your_password"
# Optional, only when jellyfin.enabled=true:
export JELLYFIN_API_KEY="your_jellyfin_api_key"
# Optional, only when tmdb.enabled=true to fill missing IDs:
export TMDB_API_KEY="your_tmdb_v3_key"
```

Then edit `config.yml`:

```yaml
provider:
  server_url: "http://provider.example.com"
  username_env: "XTREAM_USERNAME"
  password_env: "XTREAM_PASSWORD"
  user_agent: "TiviMate/5.1.6 (Android 12)"

selected_groups_file: "selected-groups.json"

output:
  movies_dir: "/mnt/nas/strm/movies"
  series_dir: "/mnt/nas/strm/tvshows"
  append_tmdb_id: true
  generate_nfo: true
  clean: false
  dry_run: false

series:
  # Use "api" when /get.php playlists are blocked but player_api.php works.
  source: "m3u"
  require_selected_m3u_group: true

tmdb:
  enabled: false
  api_key_env: "TMDB_API_KEY"
  cache_file: ".tmdb-cache.json"
  lookup_missing_only: true
  fail_on_error: false

jellyfin:
  enabled: false
  server_url: "http://jellyfin.example.com:8096"
  api_key_env: "JELLYFIN_API_KEY"
  scan_on_complete: true
```

## Static Browser Config Builder

The Docker app serves [web/group-picker.html](web/group-picker.html) with backend APIs. The same file can still be opened directly in a browser to build config files manually, but generator runs and server-side playlist fetching require the Docker backend.

The page lets you enter provider settings, output paths, TMDB/Jellyfin options, load or paste an M3U playlist, select movie and series groups, then download:

- `config.yml`
- `selected-groups.json`
- `.env`
- `run-vod-strm-builder.sh`

When served by Docker, playlist fetching runs through the local backend so browser CORS does not matter. If you open the HTML file directly, use upload/paste instead of backend actions.

## Selected Groups And Catalog

The selected groups file is a JSON object with group/category names or category IDs:

```json
{
  "movie_groups": ["|EN| ACTION/THRILLER", "|EN| NEW RELEASED"],
  "series_groups": ["|EN| NETFLIX", "|EN| TOP SERIES"],
  "movie_category_ids": [],
  "series_category_ids": []
}
```

Group/category names should match the names from the provider. Category IDs are also supported if you prefer stable numeric IDs.

An optional catalog file can be used when you already have a curated list of movie and series metadata. Set `catalog_file: "selected-catalog.json"` in `config.yml`. With this enabled, the generator uses that file for title, year, stream ID, and TMDB metadata, then uses the M3U playlist for episode URLs.

The catalog JSON shape is:

```json
{
  "metadata": {"source": "example"},
  "movies": [
    {
      "name": "The Matrix",
      "stream_id": "12345",
      "category_id": "10",
      "extension": "mp4",
      "year": 1999,
      "tmdb_id": "603",
      "imdb_id": "tt0133093"
    }
  ],
  "series": [
    {
      "name": "Example Show",
      "series_id": "67890",
      "category_id": "20",
      "year": 2024,
      "tmdb_id": "123456"
    }
  ]
}
```

## Generate

```bash
vod-strm-builder generate --config config.yml --summary-json last-run.json
```

Use `dry_run: true` first if you want a summary without writing files.

Use `clean: true` only when the output paths are dedicated to generated STRM/NFO files. It removes the existing output tree before rebuilding it.

## TMDB IDs

The normal path does not need a TMDB API key. The tool appends `{tmdb-NNN}` from a known `tmdb_id` in the provider data or optional catalog file.

The run summary reports:

- `movies_with_provider_tmdb_id`
- `series_with_provider_tmdb_id`

Optional: when `tmdb.enabled` is true, the generator can fill missing TMDB IDs before writing folders. That optional fallback uses:

- `/find/{imdb_id}` when the catalogue has an IMDb ID
- `/search/movie` or `/search/tv` using title and year otherwise

That fallback needs a real TMDB API key, read from `TMDB_API_KEY` by default. It is never stored in generated `.strm` or `.nfo` files.

`lookup_missing_only: true` keeps existing exported TMDB IDs and only calls TMDB for missing ones. Use `cache_file: ".tmdb-cache.json"` to avoid repeating lookups on later runs.

`fail_on_error: false` keeps generation running if TMDB is unavailable or the key is rejected; the summary will report `tmdb_errors`.

## Jellyfin Scan

When `jellyfin.enabled` is true, the generator calls Jellyfin after files are written:

- if `library_item_ids` is empty, it posts to `/Library/Refresh`
- if `library_item_ids` is set, it refreshes those items recursively

The Jellyfin API token is read from `JELLYFIN_API_KEY` by default. Dry runs skip the Jellyfin call.

## Notes

- The tool writes direct provider URLs into `.strm` files. Any server that scans the output library must be able to reach the provider URL.
- Movie and series filtering is based on the selected Xtream category/group names or category IDs.
- Series matching uses the `get_series` catalogue for TMDB/folder metadata and the M3U playlist for episode stream URLs.
- If a provider has inconsistent series names between `get_series` and M3U entries, those episodes are skipped and reported in the summary.
