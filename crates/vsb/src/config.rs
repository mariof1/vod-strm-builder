use std::{collections::HashMap, env, path::Path};

use anyhow::{Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use tokio::fs;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct SourceConfig {
    pub metadata: MetadataConfig,
    pub providers: Vec<ProviderConfig>,
    pub inputs: Vec<InputConfig>,
    pub sources: Vec<SourceRoute>,
}

impl SourceConfig {
    pub async fn from_file(path: &Path) -> Result<Self> {
        let raw = fs::read_to_string(path)
            .await
            .with_context(|| format!("read source config {}", path.display()))?;
        Self::from_str(&raw, &path.display().to_string())
    }

    pub fn from_str(raw: &str, label: &str) -> Result<Self> {
        let expanded = expand_env(&raw)?;
        let config = serde_yaml::from_str::<SourceConfig>(&expanded)
            .with_context(|| format!("parse source config {label}"))?;
        config.validate()?;
        Ok(config)
    }

    pub fn to_yaml(&self) -> Result<String> {
        serde_yaml::to_string(self).context("serialize source config")
    }

    pub fn validate_config(&self) -> Result<()> {
        self.validate()
    }

    fn validate(&self) -> Result<()> {
        let mut provider_names = HashMap::new();
        for provider in &self.providers {
            if provider.name.trim().is_empty() {
                anyhow::bail!("provider name is required");
            }
            if provider_names
                .insert(provider.name.as_str(), true)
                .is_some()
            {
                anyhow::bail!("duplicate provider name {}", provider.name);
            }
            if provider.urls.is_empty() {
                anyhow::bail!("provider {} needs at least one url", provider.name);
            }
        }
        let mut input_names = HashMap::new();
        for input in &self.inputs {
            if input.name.trim().is_empty() {
                anyhow::bail!("input name is required");
            }
            if input_names.insert(input.name.as_str(), true).is_some() {
                anyhow::bail!("duplicate input name {}", input.name);
            }
            if input.url.trim().is_empty() && input.provider.is_none() {
                anyhow::bail!("input {} requires url or provider", input.name);
            }
            if let Some(provider) = input.provider.as_deref() {
                if !provider_names.contains_key(provider) {
                    anyhow::bail!(
                        "input {} references unknown provider {}",
                        input.name,
                        provider
                    );
                }
            }
            if let Some(provider) = provider_name_from_url(&input.url) {
                if !provider_names.contains_key(provider) {
                    anyhow::bail!(
                        "input {} references unknown provider {}",
                        input.name,
                        provider
                    );
                }
            }
            if matches!(input.kind, InputKind::Xtream)
                && (input.username.is_none() || input.password.is_none())
            {
                anyhow::bail!("xtream input {} requires username and password", input.name);
            }
        }
        for route in &self.sources {
            for input in &route.inputs {
                if !input_names.contains_key(input.as_str()) {
                    anyhow::bail!("source references unknown input {}", input);
                }
            }
            for target in &route.targets {
                if target.outputs.is_empty() {
                    anyhow::bail!("target {} needs at least one output", target.name);
                }
            }
        }
        Ok(())
    }

    pub fn resolved_urls(&self, input: &InputConfig) -> Result<Vec<String>> {
        if let Some((provider_name, path)) = provider_ref_from_url(&input.url) {
            let provider = self.provider(provider_name)?;
            return Ok(provider
                .urls
                .iter()
                .map(|base| join_provider_url(base, path))
                .collect());
        }
        if let Some(provider_name) = input.provider.as_deref() {
            let provider = self.provider(provider_name)?;
            let path = input.url.trim();
            return Ok(provider
                .urls
                .iter()
                .map(|base| join_provider_url(base, path))
                .collect());
        }
        Ok(vec![input.url.clone()])
    }

    fn provider(&self, name: &str) -> Result<&ProviderConfig> {
        self.providers
            .iter()
            .find(|provider| provider.name == name)
            .with_context(|| format!("unknown provider {name}"))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct MetadataConfig {
    pub tmdb: TmdbConfig,
    pub jellyfin: JellyfinConfig,
}

impl Default for MetadataConfig {
    fn default() -> Self {
        Self {
            tmdb: TmdbConfig::default(),
            jellyfin: JellyfinConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TmdbConfig {
    pub enabled: bool,
    pub api_key: Option<String>,
    pub api_key_env: String,
    pub language: String,
    pub cache_file: String,
    pub lookup_missing_only: bool,
    pub fail_on_error: bool,
}

impl Default for TmdbConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            api_key: None,
            api_key_env: "TMDB_API_KEY".to_string(),
            language: "en-US".to_string(),
            cache_file: ".tmdb-cache.json".to_string(),
            lookup_missing_only: true,
            fail_on_error: false,
        }
    }
}

impl TmdbConfig {
    pub fn api_key_value(&self) -> Option<String> {
        self.api_key
            .as_ref()
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
            .or_else(|| {
                env::var(&self.api_key_env)
                    .ok()
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
            })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct JellyfinConfig {
    pub enabled: bool,
    pub server_url: String,
    pub api_key: Option<String>,
    pub api_key_env: String,
    pub library_item_ids: Vec<String>,
    pub scan_on_complete: bool,
}

impl Default for JellyfinConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            server_url: String::new(),
            api_key: None,
            api_key_env: "JELLYFIN_API_KEY".to_string(),
            library_item_ids: Vec::new(),
            scan_on_complete: true,
        }
    }
}

impl JellyfinConfig {
    pub fn api_key_value(&self) -> Option<String> {
        self.api_key
            .as_ref()
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
            .or_else(|| {
                env::var(&self.api_key_env)
                    .ok()
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
            })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ProviderConfig {
    pub name: String,
    pub urls: Vec<String>,
    pub provider_url_selection_policy: ProviderSelectionPolicy,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            name: String::new(),
            urls: Vec::new(),
            provider_url_selection_policy: ProviderSelectionPolicy::ResumeLastWorking,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ProviderSelectionPolicy {
    #[default]
    ResumeLastWorking,
    RestartFromFirst,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct InputConfig {
    pub name: String,
    #[serde(rename = "type")]
    pub kind: InputKind,
    pub provider: Option<String>,
    pub url: String,
    pub username: Option<String>,
    pub password: Option<String>,
    pub enabled: bool,
    pub headers: HashMap<String, String>,
    pub options: InputOptions,
}

impl Default for InputConfig {
    fn default() -> Self {
        Self {
            name: String::new(),
            kind: InputKind::M3u,
            provider: None,
            url: String::new(),
            username: None,
            password: None,
            enabled: true,
            headers: HashMap::new(),
            options: InputOptions::default(),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum InputKind {
    #[default]
    M3u,
    Xtream,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct InputOptions {
    pub user_agent: String,
    pub xtream_skip_live: bool,
    pub xtream_skip_vod: bool,
    pub xtream_skip_series: bool,
    pub xtream_series_source: XtreamSeriesSource,
}

impl Default for InputOptions {
    fn default() -> Self {
        Self {
            user_agent: "TiviMate/5.1.6 (Android 12)".to_string(),
            xtream_skip_live: true,
            xtream_skip_vod: false,
            xtream_skip_series: false,
            xtream_series_source: XtreamSeriesSource::M3u,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum XtreamSeriesSource {
    Api,
    #[default]
    M3u,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct SourceRoute {
    pub inputs: Vec<String>,
    pub targets: Vec<TargetConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TargetConfig {
    pub name: String,
    pub enabled: bool,
    pub filter: Option<String>,
    pub groups: Vec<String>,
    #[serde(rename = "output")]
    pub outputs: Vec<OutputConfig>,
}

impl Default for TargetConfig {
    fn default() -> Self {
        Self {
            name: "default".to_string(),
            enabled: true,
            filter: None,
            groups: Vec::new(),
            outputs: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum OutputConfig {
    M3u(M3uOutput),
    Strm(StrmOutput),
    Xtream(XtreamOutput),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct M3uOutput {
    pub filename: String,
    pub filter: Option<String>,
}

impl Default for M3uOutput {
    fn default() -> Self {
        Self {
            filename: "playlist.m3u".to_string(),
            filter: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct StrmOutput {
    pub directory: String,
    pub movies_directory: String,
    pub series_directory: String,
    pub style: StrmStyle,
    pub append_tmdb_id: bool,
    pub generate_nfo: bool,
    pub incremental: bool,
    pub cleanup: bool,
    pub dry_run: bool,
    pub filter: Option<String>,
}

impl Default for StrmOutput {
    fn default() -> Self {
        Self {
            directory: "/media".to_string(),
            movies_directory: "movies".to_string(),
            series_directory: "tvshows".to_string(),
            style: StrmStyle::Jellyfin,
            append_tmdb_id: true,
            generate_nfo: true,
            incremental: true,
            cleanup: false,
            dry_run: true,
            filter: None,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum StrmStyle {
    Kodi,
    Emby,
    #[default]
    Jellyfin,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct XtreamOutput {
    pub directory: String,
    pub filter: Option<String>,
}

fn expand_env(raw: &str) -> Result<String> {
    let re = Regex::new(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")?;
    Ok(re
        .replace_all(raw, |caps: &regex::Captures<'_>| {
            env::var(&caps[1]).unwrap_or_else(|_| caps[0].to_string())
        })
        .to_string())
}

fn provider_name_from_url(url: &str) -> Option<&str> {
    provider_ref_from_url(url).map(|(name, _)| name)
}

fn provider_ref_from_url(url: &str) -> Option<(&str, &str)> {
    let rest = url.trim().strip_prefix("provider://")?;
    match rest.split_once('/') {
        Some((name, path)) => Some((name, path)),
        None => Some((rest, "")),
    }
}

fn join_provider_url(base: &str, path: &str) -> String {
    let clean_base = base.trim_end_matches('/');
    let clean_path = path.trim();
    if clean_path.is_empty() {
        clean_base.to_string()
    } else if clean_path.starts_with("http://") || clean_path.starts_with("https://") {
        clean_path.to_string()
    } else if clean_path.starts_with('/') {
        format!("{clean_base}{clean_path}")
    } else {
        format!("{clean_base}/{clean_path}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_provider_urls_in_order() {
        let config = SourceConfig {
            metadata: MetadataConfig::default(),
            providers: vec![ProviderConfig {
                name: "main".to_string(),
                urls: vec![
                    "http://primary.example".to_string(),
                    "http://backup.example/".to_string(),
                ],
                ..ProviderConfig::default()
            }],
            inputs: vec![],
            sources: vec![],
        };
        let input = InputConfig {
            name: "playlist".to_string(),
            provider: Some("main".to_string()),
            url: "/get.php?type=m3u_plus".to_string(),
            ..InputConfig::default()
        };
        assert_eq!(
            config.resolved_urls(&input).unwrap(),
            vec![
                "http://primary.example/get.php?type=m3u_plus",
                "http://backup.example/get.php?type=m3u_plus"
            ]
        );
    }

    #[test]
    fn resolves_provider_scheme_urls() {
        let config = SourceConfig {
            metadata: MetadataConfig::default(),
            providers: vec![ProviderConfig {
                name: "main".to_string(),
                urls: vec!["http://primary.example".to_string()],
                ..ProviderConfig::default()
            }],
            inputs: vec![],
            sources: vec![],
        };
        let input = InputConfig {
            name: "playlist".to_string(),
            url: "provider://main/get.php".to_string(),
            ..InputConfig::default()
        };
        assert_eq!(
            config.resolved_urls(&input).unwrap(),
            vec!["http://primary.example/get.php"]
        );
    }

    #[test]
    fn keeps_missing_env_placeholders() {
        let raw = "username: ${env:VSB_TEST_MISSING_ENV}\n";
        assert_eq!(expand_env(raw).unwrap(), raw);
    }
}
