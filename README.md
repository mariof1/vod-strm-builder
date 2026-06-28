# VOD STRM Builder

Generate a media-server-friendly VOD library directly from an Xtream/M3U provider.

It writes:

- movie `.strm` files and movie `.nfo`
- TV episode `.strm` files, `tvshow.nfo`, and episode `.nfo`
- folder names with Jellyfin/Plex-style TMDB suffixes, for example `The Matrix (1999) {tmdb-603}`

The important design choice is that series episodes are read from the provider's `m3u_plus` playlist instead of calling `player_api.php?action=get_series_info` for every series. That avoids slow or flaky per-series Xtream API scans.

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
```

Then edit `config.yml`:

```yaml
provider:
  server_url: "http://provider.example.com"
  username_env: "XTREAM_USERNAME"
  password_env: "XTREAM_PASSWORD"

selected_groups_file: "selected-groups.dispatcharr.json"

output:
  movies_dir: "/mnt/nas/strm/movies"
  series_dir: "/mnt/nas/strm/tvshows"
  append_tmdb_id: true
  generate_nfo: true
  clean: false
  dry_run: false

series:
  source: "m3u"
  require_selected_m3u_group: true
```

## Export Selected Dispatcharr Groups

This is a one-time bridge from your existing Dispatcharr choices into this standalone tool.

From the Docker host running Dispatcharr:

```bash
docker cp tools/export_dispatcharr_vod_groups.py dispatcharr:/tmp/export_dispatcharr_vod_groups.py
docker exec dispatcharr sh -lc "/dispatcharrpy/bin/python /app/manage.py shell < /tmp/export_dispatcharr_vod_groups.py" > selected-groups.dispatcharr.json
```

If Django startup logs appear before the JSON, remove those log lines so the file starts with `{`.

The generated file contains only group/category names and IDs, not provider credentials.

For the more robust runtime path, export the selected VOD catalogue too:

```bash
docker cp tools/export_dispatcharr_vod_catalog.py dispatcharr:/tmp/export_dispatcharr_vod_catalog.py
docker exec dispatcharr sh -lc "/dispatcharrpy/bin/python /app/manage.py shell < /tmp/export_dispatcharr_vod_catalog.py" > selected-catalog.dispatcharr.json
```

Set `catalog_file: "selected-catalog.dispatcharr.json"` in `config.yml`. With this enabled, the generator uses Dispatcharr's exported TMDB metadata and skips Xtream catalogue API calls at runtime.

## Generate

```bash
vod-strm-builder generate --config config.yml --summary-json last-run.json
```

Use `dry_run: true` first if you want a summary without writing files.

Use `clean: true` only when the output paths are dedicated to generated STRM/NFO files. It removes the existing output tree before rebuilding it.

## Notes

- The tool writes direct provider URLs into `.strm` files. Any server that scans the output library must be able to reach the provider URL.
- Movie and series filtering is based on the selected Xtream category/group names or category IDs.
- Series matching uses the `get_series` catalogue for TMDB/folder metadata and the M3U playlist for episode stream URLs.
- If a provider has inconsistent series names between `get_series` and M3U entries, those episodes are skipped and reported in the summary.
