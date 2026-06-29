use std::{collections::HashMap, path::Path};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::fs;

use crate::{
    config::TmdbConfig,
    model::{MediaItem, MediaKind},
    text::{clean_title, strip_redundant_year},
};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TmdbSummary {
    pub enabled: bool,
    pub looked_up: usize,
    pub cache_hits: usize,
    pub matched: usize,
    pub errors: usize,
}

pub async fn enrich_items(
    items: &mut [MediaItem],
    config: &TmdbConfig,
    work_dir: &Path,
) -> Result<TmdbSummary> {
    let mut summary = TmdbSummary {
        enabled: config.enabled,
        ..TmdbSummary::default()
    };
    if !config.enabled {
        return Ok(summary);
    }
    let Some(api_key) = config.api_key_value() else {
        if config.fail_on_error {
            anyhow::bail!("TMDB is enabled but no API key is configured");
        }
        summary.errors += 1;
        return Ok(summary);
    };
    let cache_path = cache_path(work_dir, &config.cache_file);
    let mut cache = read_cache(&cache_path).await.unwrap_or_default();
    let client = reqwest::Client::new();
    for item in items.iter_mut() {
        if item.kind == MediaKind::Live {
            continue;
        }
        if config.lookup_missing_only && item.tmdb_id.is_some() {
            continue;
        }
        let title = item
            .episode
            .as_ref()
            .map(|episode| episode.series_name.as_str())
            .unwrap_or(&item.name);
        let title = strip_redundant_year(&clean_title(title), item.year);
        if title.is_empty() {
            continue;
        }
        let key = format!(
            "{:?}:{}:{:?}",
            item.kind,
            title.to_ascii_lowercase(),
            item.year
        );
        if let Some(cached) = cache.get(&key) {
            summary.cache_hits += 1;
            if let Some(tmdb_id) = cached.as_ref() {
                item.tmdb_id = Some(tmdb_id.clone());
                summary.matched += 1;
            }
            continue;
        }
        summary.looked_up += 1;
        match lookup(&client, &api_key, config, item.kind, &title, item.year).await {
            Ok(tmdb_id) => {
                if let Some(tmdb_id) = tmdb_id.as_ref() {
                    item.tmdb_id = Some(tmdb_id.clone());
                    summary.matched += 1;
                }
                cache.insert(key, tmdb_id);
            }
            Err(error) => {
                summary.errors += 1;
                if config.fail_on_error {
                    return Err(error);
                }
                cache.insert(key, None);
            }
        }
    }
    write_cache(&cache_path, &cache).await?;
    Ok(summary)
}

async fn lookup(
    client: &reqwest::Client,
    api_key: &str,
    config: &TmdbConfig,
    kind: MediaKind,
    title: &str,
    year: Option<u16>,
) -> Result<Option<String>> {
    let endpoint = match kind {
        MediaKind::Movie => "https://api.themoviedb.org/3/search/movie",
        MediaKind::Series => "https://api.themoviedb.org/3/search/tv",
        MediaKind::Live => return Ok(None),
    };
    let mut request = client.get(endpoint).query(&[
        ("api_key", api_key),
        ("query", title),
        ("language", config.language.as_str()),
    ]);
    let year_value;
    if let Some(year) = year {
        year_value = year.to_string();
        request = request.query(&[(
            if kind == MediaKind::Movie {
                "year"
            } else {
                "first_air_date_year"
            },
            year_value.as_str(),
        )]);
    }
    let response = request
        .send()
        .await
        .with_context(|| format!("search TMDB for {title}"))?;
    let status = response.status();
    if !status.is_success() {
        anyhow::bail!("TMDB search failed with HTTP {status}");
    }
    let json = response.json::<Value>().await.context("parse TMDB JSON")?;
    Ok(json
        .get("results")
        .and_then(Value::as_array)
        .and_then(|results| results.first())
        .and_then(|row| row.get("id"))
        .and_then(|id| match id {
            Value::String(value) => Some(value.clone()),
            Value::Number(value) => Some(value.to_string()),
            _ => None,
        }))
}

fn cache_path(work_dir: &Path, cache_file: &str) -> std::path::PathBuf {
    let path = Path::new(cache_file);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        work_dir.join(path)
    }
}

async fn read_cache(path: &Path) -> Result<HashMap<String, Option<String>>> {
    let raw = fs::read_to_string(path)
        .await
        .with_context(|| format!("read TMDB cache {}", path.display()))?;
    serde_json::from_str(&raw).context("parse TMDB cache")
}

async fn write_cache(path: &Path, cache: &HashMap<String, Option<String>>) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create {}", parent.display()))?;
    }
    fs::write(path, serde_json::to_string_pretty(cache)?)
        .await
        .with_context(|| format!("write TMDB cache {}", path.display()))
}
