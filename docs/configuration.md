# Configuration

`source.yml` is organized around five layers:

- `metadata`: TMDB lookup and Jellyfin refresh settings.
- `providers`: reusable base URLs with ordered failover.
- `inputs`: M3U or Xtream upstream definitions.
- `sources`: routes one or more inputs into one or more targets.
- `targets.output`: writes `strm`, `m3u`, or reserved `xtream` outputs.

The browser app stores the file at `/work/source.yml`.

## Metadata

```yaml
metadata:
  tmdb:
    enabled: false
    api_key_env: TMDB_API_KEY
    language: en-US
    cache_file: .tmdb-cache.json
    lookup_missing_only: true
    fail_on_error: false
  jellyfin:
    enabled: false
    server_url: http://jellyfin.example.com:8096
    api_key_env: JELLYFIN_API_KEY
    library_item_ids: []
    scan_on_complete: true
```

`tmdb.api_key` and `jellyfin.api_key` can be set directly, but environment variables are preferred.

## Providers

```yaml
providers:
  - name: main_failover
    urls:
      - http://primary-provider.example
      - http://backup-provider.example
    provider_url_selection_policy: resume_last_working
```

Inputs can use a provider with either:

```yaml
provider: main_failover
```

or:

```yaml
url: provider://main_failover/get.php?type=m3u_plus
```

The current implementation tries provider URLs in order. Persisting the last working URL is reserved for a later cache pass.

## Inputs

M3U input:

```yaml
inputs:
  - name: playlist
    type: m3u
    url: /work/provider.m3u
```

Xtream input:

```yaml
inputs:
  - name: provider
    type: xtream
    provider: main_failover
    username: ${env:XTREAM_USERNAME}
    password: ${env:XTREAM_PASSWORD}
    options:
      xtream_skip_live: true
      xtream_skip_vod: false
      xtream_skip_series: false
      xtream_series_source: m3u
```

## Targets

```yaml
sources:
  - inputs: [provider]
    targets:
      - name: media-library
        filter: Group ~ ".*"
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
            dry_run: false
```

Initial filter expressions:

- `Group ~ "regex"`
- `Name ~ "regex"`
- `Title ~ "regex"`
- `Input ~ "regex"`
- `Url ~ "regex"`
- `Type = movie|vod|series|live`
- simple `AND`

## Outputs

STRM/NFO:

```yaml
- type: strm
  directory: /media
  movies_directory: movies
  series_directory: tvshows
```

M3U:

```yaml
- type: m3u
  filename: /work/generated/media-library.m3u
```

## Runtime Files

The app also writes these files in `/work`:

- `app-settings.json`: scheduler settings.
- `groups.json`: latest synced group counts.
- `last-run.json`: latest generation summary.
- `.tmdb-cache.json`: optional TMDB lookup cache.
