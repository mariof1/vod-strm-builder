use std::{collections::BTreeMap, fmt, path::Path};

use anyhow::{Context, Result};
use serde::Serialize;

use crate::{
    config::{InputKind, OutputConfig, SourceConfig},
    filter,
    jellyfin::{self, JellyfinSummary},
    m3u,
    model::{GroupSummary, InputSummary, MediaItem, MediaKind, ScanSummary},
    tmdb::{self, TmdbSummary},
    writer::{self, WriteSummary},
    xtream,
};

#[derive(Debug, Serialize)]
pub struct GenerateSummary {
    pub scanned: ScanSummary,
    pub targets: Vec<TargetSummary>,
    pub tmdb: TmdbSummary,
    pub jellyfin: JellyfinSummary,
}

impl fmt::Display for GenerateSummary {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(
            f,
            "scanned: {} movies, {} series episodes, {} live streams",
            self.scanned.total_movies, self.scanned.total_series_episodes, self.scanned.total_live
        )?;
        for target in &self.targets {
            writeln!(f, "target {}: {} items", target.name, target.items)?;
            for output in &target.outputs {
                writeln!(
                    f,
                    "  {}: strm={} nfo={} unchanged={} m3u={}",
                    output.kind,
                    output.summary.strm_files_written,
                    output.summary.nfo_files_written,
                    output.summary.unchanged_files,
                    output.summary.m3u_files_written
                )?;
            }
        }
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct TargetSummary {
    pub name: String,
    pub items: usize,
    pub outputs: Vec<OutputSummary>,
}

#[derive(Debug, Serialize)]
pub struct OutputSummary {
    pub kind: String,
    pub summary: WriteSummary,
}

pub async fn scan(config: &SourceConfig) -> Result<ScanSummary> {
    let items = load_all_inputs(config).await?;
    Ok(summarize(&items))
}

pub async fn generate(config: &SourceConfig, target_name: Option<&str>) -> Result<GenerateSummary> {
    generate_with_work_dir(config, target_name, Path::new(".")).await
}

pub async fn generate_with_work_dir(
    config: &SourceConfig,
    target_name: Option<&str>,
    work_dir: &Path,
) -> Result<GenerateSummary> {
    let mut items = load_all_inputs(config).await?;
    let tmdb = tmdb::enrich_items(&mut items, &config.metadata.tmdb, work_dir).await?;
    let scanned = summarize(&items);
    let mut targets = Vec::new();
    for route in &config.sources {
        let route_inputs = route
            .inputs
            .iter()
            .map(String::as_str)
            .collect::<std::collections::HashSet<_>>();
        let route_items = items
            .iter()
            .filter(|item| route_inputs.contains(item.input.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        for target in &route.targets {
            if !target.enabled {
                continue;
            }
            if target_name.is_some_and(|name| name != target.name) {
                continue;
            }
            let selected = route_items
                .iter()
                .filter(|item| {
                    filter::matches_target(item, target.filter.as_deref(), &target.groups)
                        .unwrap_or(false)
                })
                .cloned()
                .collect::<Vec<_>>();
            let mut outputs = Vec::new();
            for output in &target.outputs {
                match output {
                    OutputConfig::M3u(out) => outputs.push(OutputSummary {
                        kind: "m3u".to_string(),
                        summary: writer::write_m3u(&selected, out).await?,
                    }),
                    OutputConfig::Strm(out) => outputs.push(OutputSummary {
                        kind: "strm".to_string(),
                        summary: writer::write_strm(&selected, out).await?,
                    }),
                    OutputConfig::Xtream(_) => outputs.push(OutputSummary {
                        kind: "xtream".to_string(),
                        summary: WriteSummary {
                            skipped_items: selected.len(),
                            ..WriteSummary::default()
                        },
                    }),
                }
            }
            targets.push(TargetSummary {
                name: target.name.clone(),
                items: selected.len(),
                outputs,
            });
        }
    }
    let jellyfin = if should_refresh_jellyfin(config, target_name, &targets) {
        match jellyfin::refresh(&config.metadata.jellyfin).await {
            Ok(summary) => summary,
            Err(error) => JellyfinSummary {
                enabled: config.metadata.jellyfin.enabled,
                requested: true,
                refreshed_items: 0,
                errors: usize::from(!error.to_string().is_empty()),
            },
        }
    } else {
        JellyfinSummary {
            enabled: config.metadata.jellyfin.enabled,
            ..JellyfinSummary::default()
        }
    };
    Ok(GenerateSummary {
        scanned,
        targets,
        tmdb,
        jellyfin,
    })
}

async fn load_all_inputs(config: &SourceConfig) -> Result<Vec<MediaItem>> {
    let mut all = Vec::new();
    for input in config.inputs.iter().filter(|input| input.enabled) {
        tracing::info!(input = input.name, kind = ?input.kind, "loading input");
        let urls = config.resolved_urls(input)?;
        let mut items = load_input_with_failover(input, &urls).await?;
        tracing::info!(input = input.name, items = items.len(), "loaded input");
        all.append(&mut items);
    }
    Ok(all)
}

async fn load_input_with_failover(
    input: &crate::config::InputConfig,
    urls: &[String],
) -> Result<Vec<MediaItem>> {
    let mut last_error = None;
    for url in urls {
        tracing::info!(input = input.name, url = %redact_url(url), "trying input url");
        let result = match input.kind {
            InputKind::M3u => m3u::load_input(input, url).await,
            InputKind::Xtream => xtream::load_input(input, url).await,
        };
        match result {
            Ok(items) => return Ok(items),
            Err(error) => {
                tracing::warn!(
                    input = input.name,
                    url = %redact_url(url),
                    error = %error,
                    "input url failed"
                );
                last_error = Some(error);
            }
        }
    }
    Err(last_error.unwrap_or_else(|| anyhow::anyhow!("no urls resolved for {}", input.name)))
        .with_context(|| format!("load input {}", input.name))
}

fn redact_url(url: &str) -> String {
    let Ok(mut parsed) = url::Url::parse(url) else {
        return url.to_string();
    };
    if parsed.query_pairs().any(|(key, _)| {
        key.eq_ignore_ascii_case("password") || key.eq_ignore_ascii_case("username")
    }) {
        let filtered = parsed
            .query_pairs()
            .map(|(key, value)| {
                let value = if key.eq_ignore_ascii_case("password")
                    || key.eq_ignore_ascii_case("username")
                {
                    "***".into()
                } else {
                    value
                };
                (key.into_owned(), value.into_owned())
            })
            .collect::<Vec<_>>();
        parsed.query_pairs_mut().clear().extend_pairs(filtered);
    }
    parsed.to_string()
}

fn summarize(items: &[MediaItem]) -> ScanSummary {
    let mut by_input: BTreeMap<String, Vec<&MediaItem>> = BTreeMap::new();
    for item in items {
        by_input.entry(item.input.clone()).or_default().push(item);
    }
    let inputs = by_input
        .into_iter()
        .map(|(name, items)| {
            let mut groups: BTreeMap<String, GroupSummary> = BTreeMap::new();
            let mut live = 0usize;
            let mut movies = 0usize;
            let mut series_episodes = 0usize;
            for item in items {
                let group = groups
                    .entry(item.group.clone())
                    .or_insert_with(|| GroupSummary {
                        name: item.group.clone(),
                        live: 0,
                        movies: 0,
                        series_episodes: 0,
                    });
                match item.kind {
                    MediaKind::Live => {
                        live += 1;
                        group.live += 1;
                    }
                    MediaKind::Movie => {
                        movies += 1;
                        group.movies += 1;
                    }
                    MediaKind::Series => {
                        series_episodes += 1;
                        group.series_episodes += 1;
                    }
                }
            }
            InputSummary {
                name,
                live,
                movies,
                series_episodes,
                groups: groups.into_values().collect(),
            }
        })
        .collect::<Vec<_>>();
    ScanSummary {
        total_live: inputs.iter().map(|input| input.live).sum(),
        total_movies: inputs.iter().map(|input| input.movies).sum(),
        total_series_episodes: inputs.iter().map(|input| input.series_episodes).sum(),
        inputs,
    }
}

fn should_refresh_jellyfin(
    config: &SourceConfig,
    target_name: Option<&str>,
    targets: &[TargetSummary],
) -> bool {
    if !config.metadata.jellyfin.enabled {
        return false;
    }
    let wrote_files = targets.iter().any(|target| {
        target.outputs.iter().any(|output| {
            output.summary.strm_files_written > 0
                || output.summary.nfo_files_written > 0
                || output.summary.m3u_files_written > 0
        })
    });
    if !wrote_files {
        return false;
    }
    config.sources.iter().any(|route| {
        route.targets.iter().any(|target| {
            target.enabled
                && target_name.is_none_or(|name| name == target.name)
                && target.outputs.iter().any(|output| match output {
                    OutputConfig::Strm(out) => !out.dry_run,
                    _ => false,
                })
        })
    })
}

impl fmt::Display for ScanSummary {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(
            f,
            "{} movies, {} series episodes, {} live streams",
            self.total_movies, self.total_series_episodes, self.total_live
        )?;
        for input in &self.inputs {
            writeln!(
                f,
                "{}: {} movies, {} series episodes, {} live streams, {} groups",
                input.name,
                input.movies,
                input.series_episodes,
                input.live,
                input.groups.len()
            )?;
        }
        Ok(())
    }
}
