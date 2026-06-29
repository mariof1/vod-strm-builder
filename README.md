# VOD STRM Builder

Rust-only IPTV VOD/series library builder with a browser UI, background jobs, scheduler, and Tuliprox-style source-target-output configuration.

It reads M3U or Xtream sources, syncs group/category counts, lets you choose groups, and generates media-server-friendly output:

- movie `.strm` and `.nfo` files
- TV episode `.strm`, `tvshow.nfo`, and episode `.nfo` files
- generated M3U playlists
- Jellyfin/Plex-style folders such as `The Matrix (1999) {tmdb-603}`

## Run

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

The container uses:

```text
/work                 persistent app state, source.yml, job summaries
/media/movies         generated movie library
/media/tvshows        generated TV library
```

## App Workflow

1. Add one or more provider base URLs.
2. Add M3U or Xtream inputs.
3. Set output paths and metadata options.
4. Click `Sync` to fetch groups into `/work/groups.json`.
5. Select groups.
6. Click `Generate`.

Jobs run in the background and expose progress, logs, cancellation, and previous run summaries.

## Configuration

The app persists the main configuration as `/work/source.yml`. The same file can be used by the CLI.

```yaml
providers:
  - name: main_failover
    urls:
      - http://primary-provider.example
      - http://backup-provider.example

inputs:
  - name: main
    type: xtream
    provider: main_failover
    username: ${env:XTREAM_USERNAME}
    password: ${env:XTREAM_PASSWORD}
    options:
      xtream_skip_live: true
      xtream_series_source: m3u

sources:
  - inputs: [main]
    targets:
      - name: media-library
        groups: []
        output:
          - type: strm
            directory: /media
            movies_directory: movies
            series_directory: tvshows
            append_tmdb_id: true
            generate_nfo: true
            incremental: true
            cleanup: false
            dry_run: true
```

See [docs/configuration.md](docs/configuration.md) and [examples/source.rust.example.yml](examples/source.rust.example.yml).

## Metadata

Provider TMDB IDs are used automatically when present.

Optional TMDB lookup can fill missing IDs:

```yaml
metadata:
  tmdb:
    enabled: true
    api_key_env: TMDB_API_KEY
    cache_file: .tmdb-cache.json
```

Optional Jellyfin refresh can run after a non-dry-run generation:

```yaml
metadata:
  jellyfin:
    enabled: true
    server_url: http://jellyfin.example.com:8096
    api_key_env: JELLYFIN_API_KEY
    scan_on_complete: true
```

## CLI

Print an example config:

```bash
cargo run -p vod-strm-builder --bin vod-strm-builder-rs -- example
```

Scan inputs:

```bash
cargo run -p vod-strm-builder --bin vod-strm-builder-rs -- \
  scan --source examples/source.rust.example.yml --json
```

Generate outputs:

```bash
cargo run -p vod-strm-builder --bin vod-strm-builder-rs -- \
  generate --source source.yml --json
```

Run the web app:

```bash
cargo run -p vod-strm-builder --bin vod-strm-builder-rs -- \
  serve --work-dir ./work --bind 0.0.0.0:8080
```

## Portainer

GitHub Actions publishes:

```text
mars148/vod-strm-builder:latest
```

Use [deploy/portainer-stack.yml](deploy/portainer-stack.yml). The stack maps:

```text
/opt/vod-strm-builder/work -> /work
/mnt/strm/movies -> /media/movies
/mnt/strm/tvshows -> /media/tvshows
```

## Safety

- Keep `dry_run: true` until the selected groups and output paths look right.
- Use `cleanup: true` only when the output folders are dedicated to generated `.strm` and `.nfo` files.
- Prefer environment variables for secrets: `XTREAM_USERNAME`, `XTREAM_PASSWORD`, `TMDB_API_KEY`, and `JELLYFIN_API_KEY`.
