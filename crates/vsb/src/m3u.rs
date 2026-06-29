use std::{collections::HashMap, path::Path};

use anyhow::{Context, Result};
use regex::Regex;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue, USER_AGENT};
use tokio::fs;

use crate::{
    config::InputConfig,
    model::{EpisodeInfo, MediaItem, MediaKind},
    text::extract_year,
};

pub async fn load_input(input: &InputConfig, url: &str) -> Result<Vec<MediaItem>> {
    let text = read_source(url, input).await?;
    parse_playlist(&text, &input.name)
}

pub fn parse_playlist(text: &str, input_name: &str) -> Result<Vec<MediaItem>> {
    let attr_re = Regex::new(r#"([\w-]+)="([^"]*)""#)?;
    let movie_url_re = Regex::new(r"/movie/[^/]+/[^/]+/(?P<stream>[^/.?#]+)\.(?P<ext>[^/?#]+)")?;
    let series_url_re = Regex::new(r"/series/[^/]+/[^/]+/(?P<stream>[^/.?#]+)\.(?P<ext>[^/?#]+)")?;
    let mut items = Vec::new();
    let mut extinf: Option<&str> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with("#EXTINF") {
            extinf = Some(line);
            continue;
        }
        if line.starts_with('#') || extinf.is_none() {
            continue;
        }
        let info = extinf.take().unwrap_or_default();
        let attrs = parse_attrs(&attr_re, info);
        let group = attrs
            .get("group-title")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .unwrap_or("Ungrouped")
            .to_string();
        let title = attrs
            .get("tvg-name")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .or_else(|| info.split_once(',').map(|(_, title)| title.trim()))
            .unwrap_or(line)
            .to_string();
        let lower = line.to_ascii_lowercase();
        let (kind, stream_id, extension, episode) = if let Some(caps) = movie_url_re.captures(line)
        {
            (
                MediaKind::Movie,
                caps.name("stream").map(|value| value.as_str().to_string()),
                caps.name("ext").map(|value| {
                    value
                        .as_str()
                        .split('?')
                        .next()
                        .unwrap_or("mp4")
                        .to_string()
                }),
                None,
            )
        } else if let Some(caps) = series_url_re.captures(line) {
            (
                MediaKind::Series,
                caps.name("stream").map(|value| value.as_str().to_string()),
                caps.name("ext").map(|value| {
                    value
                        .as_str()
                        .split('?')
                        .next()
                        .unwrap_or("mp4")
                        .to_string()
                }),
                parse_episode_title(&title),
            )
        } else if lower.contains("/movie/") {
            (MediaKind::Movie, None, None, None)
        } else if lower.contains("/series/") {
            (MediaKind::Series, None, None, parse_episode_title(&title))
        } else {
            (MediaKind::Live, None, None, None)
        };
        let item_name = episode
            .as_ref()
            .map(|episode| episode.series_name.clone())
            .unwrap_or_else(|| title.clone());
        let year = extract_year(&item_name);
        items.push(MediaItem {
            input: input_name.to_string(),
            kind,
            group,
            name: item_name,
            url: line.to_string(),
            stream_id,
            extension,
            year,
            tmdb_id: None,
            logo: attrs
                .get("tvg-logo")
                .cloned()
                .filter(|value| !value.is_empty()),
            episode,
        });
    }
    Ok(items)
}

pub async fn read_source(url: &str, input: &InputConfig) -> Result<String> {
    if url.starts_with("http://") || url.starts_with("https://") {
        let client = reqwest::Client::builder()
            .default_headers(headers(input)?)
            .build()?;
        let response = client
            .get(url)
            .send()
            .await
            .with_context(|| format!("download m3u input {}", input.name))?;
        let status = response.status();
        if !status.is_success() {
            anyhow::bail!(
                "download m3u input {} failed with HTTP {}",
                input.name,
                status
            );
        }
        response.text().await.context("read m3u response body")
    } else {
        let path = url.strip_prefix("file://").unwrap_or(url);
        fs::read_to_string(Path::new(path))
            .await
            .with_context(|| format!("read m3u file {path}"))
    }
}

fn headers(input: &InputConfig) -> Result<HeaderMap> {
    let mut headers = HeaderMap::new();
    headers.insert(
        USER_AGENT,
        HeaderValue::from_str(&input.options.user_agent)?,
    );
    for (key, value) in &input.headers {
        headers.insert(
            HeaderName::from_bytes(key.as_bytes())?,
            HeaderValue::from_str(value)?,
        );
    }
    Ok(headers)
}

fn parse_attrs(re: &Regex, text: &str) -> HashMap<String, String> {
    re.captures_iter(text)
        .filter_map(|caps| {
            Some((
                caps.get(1)?.as_str().to_string(),
                caps.get(2)?.as_str().to_string(),
            ))
        })
        .collect()
}

pub fn parse_episode_title(title: &str) -> Option<EpisodeInfo> {
    let patterns = [
        r"(?i)^(?P<base>.+?)\s+[Ss](?P<s>\d{1,2})\s*[ ._-]*\s*[Ee](?P<e>\d{1,4})(?P<tail>.*)$",
        r"(?i)^(?P<base>.+?)\s+(?P<s>\d{1,2})x(?P<e>\d{1,4})(?P<tail>.*)$",
    ];
    for pattern in patterns {
        let re = Regex::new(pattern).ok()?;
        let Some(caps) = re.captures(title) else {
            continue;
        };
        let series_name = caps.name("base")?.as_str().trim().to_string();
        let season = caps.name("s")?.as_str().parse::<u16>().ok()?;
        let episode = caps.name("e")?.as_str().parse::<u16>().ok()?;
        let tail = caps
            .name("tail")
            .map(|value| {
                value
                    .as_str()
                    .trim_matches(&[' ', '-', '.', '_'][..])
                    .trim()
                    .to_string()
            })
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| format!("Episode {episode:02}"));
        return Some(EpisodeInfo {
            series_name,
            season,
            episode,
            title: tail,
        });
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_groups_and_series() {
        let text = r#"#EXTM3U
#EXTINF:-1 tvg-name="EN - Dune Part Two 4K (2024)" group-title="|EN| 4K MOVIES",EN - Dune Part Two
http://x/movie/u/p/1.mp4
#EXTINF:-1 tvg-name="NF - 1899 4K S01E01" group-title="|EN| SERIES",NF - 1899 4K S01E01
http://x/series/u/p/2.mp4
"#;
        let items = parse_playlist(text, "test").unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].kind, MediaKind::Movie);
        assert_eq!(items[1].kind, MediaKind::Series);
        assert_eq!(items[1].episode.as_ref().unwrap().season, 1);
    }
}
