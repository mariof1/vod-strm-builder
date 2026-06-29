use anyhow::{Context, Result};
use reqwest::header::{HeaderMap, HeaderValue};
use serde::{Deserialize, Serialize};

use crate::config::JellyfinConfig;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct JellyfinSummary {
    pub enabled: bool,
    pub requested: bool,
    pub refreshed_items: usize,
    pub errors: usize,
}

pub async fn refresh(config: &JellyfinConfig) -> Result<JellyfinSummary> {
    let mut summary = JellyfinSummary {
        enabled: config.enabled,
        ..JellyfinSummary::default()
    };
    if !config.enabled || !config.scan_on_complete {
        return Ok(summary);
    }
    if config.server_url.trim().is_empty() {
        summary.errors += 1;
        return Ok(summary);
    }
    let Some(api_key) = config.api_key_value() else {
        summary.errors += 1;
        return Ok(summary);
    };
    let mut headers = HeaderMap::new();
    headers.insert("X-Emby-Token", HeaderValue::from_str(&api_key)?);
    let client = reqwest::Client::builder()
        .default_headers(headers)
        .build()?;
    let base = config.server_url.trim_end_matches('/');
    summary.requested = true;
    if config.library_item_ids.is_empty() {
        post(&client, &format!("{base}/Library/Refresh")).await?;
        summary.refreshed_items = 1;
    } else {
        for item_id in &config.library_item_ids {
            let url = format!(
                "{base}/Items/{}/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default",
                urlencoding::encode(item_id)
            );
            post(&client, &url).await?;
            summary.refreshed_items += 1;
        }
    }
    Ok(summary)
}

async fn post(client: &reqwest::Client, url: &str) -> Result<()> {
    let response = client
        .post(url)
        .send()
        .await
        .with_context(|| format!("post Jellyfin refresh {url}"))?;
    let status = response.status();
    if !status.is_success() {
        anyhow::bail!("Jellyfin refresh failed with HTTP {status}");
    }
    Ok(())
}
