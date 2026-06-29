use anyhow::{Context, Result};
use reqwest::header::{HeaderMap, HeaderValue, USER_AGENT};
use serde_json::Value;

use crate::{
    config::{InputConfig, XtreamSeriesSource},
    m3u,
    model::{MediaItem, MediaKind},
    text::extract_year,
};

pub async fn load_input(input: &InputConfig, base_url: &str) -> Result<Vec<MediaItem>> {
    let client = XtreamClient::new(input, base_url)?;
    let mut items = Vec::new();
    if !input.options.xtream_skip_vod {
        items.extend(client.movies().await?);
    }
    if !input.options.xtream_skip_series {
        match input.options.xtream_series_source {
            XtreamSeriesSource::M3u => {
                let playlist_url = client.m3u_url();
                items.extend(
                    m3u::load_input(input, &playlist_url)
                        .await?
                        .into_iter()
                        .filter(|item| item.kind == MediaKind::Series),
                );
            }
            XtreamSeriesSource::Api => {
                items.extend(client.series().await?);
            }
        }
    }
    if !input.options.xtream_skip_live {
        items.extend(client.live().await?);
    }
    Ok(items)
}

struct XtreamClient<'a> {
    input: &'a InputConfig,
    client: reqwest::Client,
    base_url: String,
}

impl<'a> XtreamClient<'a> {
    fn new(input: &'a InputConfig, base_url: &str) -> Result<Self> {
        let mut headers = HeaderMap::new();
        headers.insert(
            USER_AGENT,
            HeaderValue::from_str(&input.options.user_agent)?,
        );
        for (key, value) in &input.headers {
            headers.insert(
                reqwest::header::HeaderName::from_bytes(key.as_bytes())?,
                HeaderValue::from_str(value)?,
            );
        }
        Ok(Self {
            input,
            client: reqwest::Client::builder()
                .default_headers(headers)
                .build()?,
            base_url: base_url.trim_end_matches('/').to_string(),
        })
    }

    async fn live(&self) -> Result<Vec<MediaItem>> {
        let categories = self.categories("get_live_categories").await?;
        let rows = self.player_api("get_live_streams", &[]).await?;
        Ok(rows
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| {
                let name = string_field(row, &["name", "title"])?;
                let stream_id = string_field(row, &["stream_id"])?;
                let category_id = string_field(row, &["category_id"]).unwrap_or_default();
                let group = categories.get(&category_id).cloned().unwrap_or(category_id);
                Some(MediaItem {
                    input: self.input.name.clone(),
                    kind: MediaKind::Live,
                    group,
                    name,
                    url: self.live_url(&stream_id),
                    stream_id: Some(stream_id),
                    extension: Some("ts".to_string()),
                    year: None,
                    tmdb_id: None,
                    logo: string_field(row, &["stream_icon", "cover"]),
                    episode: None,
                })
            })
            .collect())
    }

    async fn movies(&self) -> Result<Vec<MediaItem>> {
        let categories = self.categories("get_vod_categories").await?;
        let rows = self.player_api("get_vod_streams", &[]).await?;
        Ok(rows
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| {
                let name = string_field(row, &["name", "title"])?;
                let stream_id = string_field(row, &["stream_id"])?;
                let ext = string_field(row, &["container_extension"])
                    .unwrap_or_else(|| "mp4".to_string());
                let category_id = string_field(row, &["category_id"]).unwrap_or_default();
                let group = categories.get(&category_id).cloned().unwrap_or(category_id);
                Some(MediaItem {
                    input: self.input.name.clone(),
                    kind: MediaKind::Movie,
                    group,
                    year: extract_year(&name),
                    tmdb_id: clean_id(row.get("tmdb").or_else(|| row.get("tmdb_id"))),
                    logo: string_field(row, &["stream_icon", "cover"]),
                    url: self.movie_url(&stream_id, &ext),
                    stream_id: Some(stream_id),
                    extension: Some(ext),
                    name,
                    episode: None,
                })
            })
            .collect())
    }

    async fn series(&self) -> Result<Vec<MediaItem>> {
        let categories = self.categories("get_series_categories").await?;
        let rows = self.player_api("get_series", &[]).await?;
        let mut output = Vec::new();
        for row in rows.as_array().into_iter().flatten() {
            let Some(series_name) = string_field(row, &["name"]) else {
                continue;
            };
            let Some(series_id) = string_field(row, &["series_id"]) else {
                continue;
            };
            let category_id = string_field(row, &["category_id"]).unwrap_or_default();
            let group = categories.get(&category_id).cloned().unwrap_or(category_id);
            let info = self
                .player_api("get_series_info", &[("series_id", series_id.as_str())])
                .await?;
            for episode_row in iter_series_episode_rows(&info) {
                let Some(stream_id) = string_field(episode_row, &["id", "stream_id"]) else {
                    continue;
                };
                let season = numeric_field(episode_row, &["season"]).unwrap_or(1);
                let episode =
                    numeric_field(episode_row, &["episode_num", "episode", "num"]).unwrap_or(1);
                let ext = string_field(episode_row, &["container_extension"])
                    .unwrap_or_else(|| "mp4".to_string());
                let title = string_field(episode_row, &["title"])
                    .unwrap_or_else(|| format!("Episode {episode:02}"));
                output.push(MediaItem {
                    input: self.input.name.clone(),
                    kind: MediaKind::Series,
                    group: group.clone(),
                    name: series_name.clone(),
                    url: self.series_url(&stream_id, &ext),
                    stream_id: Some(stream_id),
                    extension: Some(ext),
                    year: extract_year(&series_name),
                    tmdb_id: clean_id(row.get("tmdb").or_else(|| row.get("tmdb_id"))),
                    logo: string_field(row, &["cover"]),
                    episode: Some(crate::model::EpisodeInfo {
                        series_name: series_name.clone(),
                        season,
                        episode,
                        title,
                    }),
                });
            }
        }
        Ok(output)
    }

    async fn categories(&self, action: &str) -> Result<std::collections::HashMap<String, String>> {
        let rows = self.player_api(action, &[]).await?;
        Ok(rows
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| {
                Some((
                    string_field(row, &["category_id"])?,
                    string_field(row, &["category_name"])?,
                ))
            })
            .collect())
    }

    async fn player_api(&self, action: &str, extra: &[(&str, &str)]) -> Result<Value> {
        let username = self.input.username.as_deref().unwrap_or_default();
        let password = self.input.password.as_deref().unwrap_or_default();
        let mut query = vec![
            ("username", username),
            ("password", password),
            ("action", action),
        ];
        query.extend_from_slice(extra);
        let response = self
            .client
            .get(format!("{}/player_api.php", self.base_url))
            .query(&query)
            .send()
            .await
            .with_context(|| format!("xtream action {action} for {}", self.input.name))?;
        let status = response.status();
        if !status.is_success() {
            anyhow::bail!("xtream action {action} failed with HTTP {status}");
        }
        response
            .json::<Value>()
            .await
            .with_context(|| format!("parse xtream action {action} JSON"))
    }

    fn m3u_url(&self) -> String {
        format!(
            "{}/get.php?username={}&password={}&type=m3u_plus&output=ts",
            self.base_url,
            urlencoding::encode(self.input.username.as_deref().unwrap_or_default()),
            urlencoding::encode(self.input.password.as_deref().unwrap_or_default())
        )
    }

    fn live_url(&self, stream_id: &str) -> String {
        format!(
            "{}/live/{}/{}/{}.ts",
            self.base_url,
            urlencoding::encode(self.input.username.as_deref().unwrap_or_default()),
            urlencoding::encode(self.input.password.as_deref().unwrap_or_default()),
            urlencoding::encode(stream_id)
        )
    }

    fn movie_url(&self, stream_id: &str, ext: &str) -> String {
        format!(
            "{}/movie/{}/{}/{}.{}",
            self.base_url,
            urlencoding::encode(self.input.username.as_deref().unwrap_or_default()),
            urlencoding::encode(self.input.password.as_deref().unwrap_or_default()),
            urlencoding::encode(stream_id),
            ext.trim_start_matches('.')
        )
    }

    fn series_url(&self, stream_id: &str, ext: &str) -> String {
        format!(
            "{}/series/{}/{}/{}.{}",
            self.base_url,
            urlencoding::encode(self.input.username.as_deref().unwrap_or_default()),
            urlencoding::encode(self.input.password.as_deref().unwrap_or_default()),
            urlencoding::encode(stream_id),
            ext.trim_start_matches('.')
        )
    }
}

fn string_field(row: &Value, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| row.get(key))
        .and_then(|value| match value {
            Value::String(text) => Some(text.trim().to_string()),
            Value::Number(number) => Some(number.to_string()),
            _ => None,
        })
        .filter(|value| !value.is_empty() && value != "0")
}

fn numeric_field(row: &Value, keys: &[&str]) -> Option<u16> {
    keys.iter()
        .find_map(|key| row.get(key))
        .and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_str()?.parse::<u64>().ok())
        })
        .and_then(|value| u16::try_from(value).ok())
}

fn clean_id(value: Option<&Value>) -> Option<String> {
    value
        .and_then(|value| match value {
            Value::String(text) => Some(text.trim().to_string()),
            Value::Number(number) => Some(number.to_string()),
            _ => None,
        })
        .filter(|value| !value.is_empty() && value != "0")
}

fn iter_series_episode_rows(info: &Value) -> Vec<&Value> {
    let Some(episodes) = info.get("episodes").and_then(Value::as_object) else {
        return Vec::new();
    };
    episodes
        .values()
        .flat_map(|season| season.as_array().into_iter().flatten())
        .collect()
}
