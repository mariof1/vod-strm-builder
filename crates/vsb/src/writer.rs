use std::{
    collections::{BTreeMap, HashSet},
    path::{Path, PathBuf},
};

use anyhow::{Context, Result};
use tokio::fs;

use crate::{
    config::{M3uOutput, StrmOutput},
    filter,
    model::{MediaItem, MediaKind},
    text::{clean_title, folder_name, safe_filename, strip_redundant_year},
};

#[derive(Debug, Default, serde::Serialize)]
pub struct WriteSummary {
    pub m3u_files_written: usize,
    pub strm_files_written: usize,
    pub nfo_files_written: usize,
    pub unchanged_files: usize,
    pub skipped_items: usize,
}

pub async fn write_m3u(items: &[MediaItem], output: &M3uOutput) -> Result<WriteSummary> {
    let mut body = String::from("#EXTM3U\n");
    let mut count = 0usize;
    for item in items {
        if !filter::matches_filter(item, output.filter.as_deref())? {
            continue;
        }
        let logo = item.logo.as_deref().unwrap_or_default();
        body.push_str(&format!(
            "#EXTINF:-1 tvg-name=\"{}\" tvg-logo=\"{}\" group-title=\"{}\",{}\n{}\n",
            escape_attr(&item.name),
            escape_attr(logo),
            escape_attr(&item.group),
            item.name,
            item.url
        ));
        count += 1;
    }
    write_text(Path::new(&output.filename), &body, false, true).await?;
    Ok(WriteSummary {
        m3u_files_written: usize::from(count > 0),
        ..WriteSummary::default()
    })
}

pub async fn write_strm(items: &[MediaItem], output: &StrmOutput) -> Result<WriteSummary> {
    let mut summary = WriteSummary::default();
    let root = PathBuf::from(&output.directory);
    let movie_root = root.join(&output.movies_directory);
    let series_root = root.join(&output.series_directory);
    let mut written_paths = HashSet::new();
    let mut desired_paths = HashSet::new();
    let mut tvshow_nfo_written = HashSet::new();
    for item in items {
        if !filter::matches_filter(item, output.filter.as_deref())? {
            summary.skipped_items += 1;
            continue;
        }
        match item.kind {
            MediaKind::Movie => {
                write_movie(
                    item,
                    output,
                    &movie_root,
                    &mut written_paths,
                    &mut desired_paths,
                    &mut summary,
                )
                .await?;
            }
            MediaKind::Series => {
                write_episode(
                    item,
                    output,
                    &series_root,
                    &mut written_paths,
                    &mut desired_paths,
                    &mut tvshow_nfo_written,
                    &mut summary,
                )
                .await?;
            }
            MediaKind::Live => summary.skipped_items += 1,
        }
    }
    if output.cleanup && !output.dry_run {
        cleanup_tree(&movie_root, &desired_paths)?;
        cleanup_tree(&series_root, &desired_paths)?;
    }
    Ok(summary)
}

async fn write_movie(
    item: &MediaItem,
    output: &StrmOutput,
    movie_root: &Path,
    written_paths: &mut HashSet<PathBuf>,
    desired_paths: &mut HashSet<PathBuf>,
    summary: &mut WriteSummary,
) -> Result<()> {
    let folder = movie_root.join(folder_name(
        &item.name,
        item.year,
        item.tmdb_id.as_deref(),
        output.append_tmdb_id,
    ));
    let title = strip_redundant_year(&clean_title(&item.name), item.year);
    let file_stem = if let Some(year) = item.year {
        safe_filename(&format!("{title} ({year})"))
    } else {
        safe_filename(&title)
    };
    let strm = folder.join(format!("{file_stem}.strm"));
    desired_paths.insert(strm.clone());
    if written_paths.insert(strm.clone()) {
        count_status(
            write_text(&strm, &item.url, output.dry_run, output.incremental).await?,
            true,
            summary,
        );
    }
    if output.generate_nfo {
        let nfo = folder.join(format!("{file_stem}.nfo"));
        desired_paths.insert(nfo.clone());
        count_status(
            write_text(&nfo, &movie_nfo(item), output.dry_run, output.incremental).await?,
            false,
            summary,
        );
    }
    Ok(())
}

async fn write_episode(
    item: &MediaItem,
    output: &StrmOutput,
    series_root: &Path,
    written_paths: &mut HashSet<PathBuf>,
    desired_paths: &mut HashSet<PathBuf>,
    tvshow_nfo_written: &mut HashSet<PathBuf>,
    summary: &mut WriteSummary,
) -> Result<()> {
    let Some(episode) = item.episode.as_ref() else {
        summary.skipped_items += 1;
        return Ok(());
    };
    let folder = series_root.join(folder_name(
        &episode.series_name,
        item.year,
        item.tmdb_id.as_deref(),
        output.append_tmdb_id,
    ));
    if output.generate_nfo && tvshow_nfo_written.insert(folder.clone()) {
        desired_paths.insert(folder.join("tvshow.nfo"));
        count_status(
            write_text(
                &folder.join("tvshow.nfo"),
                &series_nfo(item),
                output.dry_run,
                output.incremental,
            )
            .await?,
            false,
            summary,
        );
    }
    let season_folder = folder.join(format!("Season {:02}", episode.season));
    let title = safe_filename(&format!(
        "{} - S{:02}E{:02} - {}",
        clean_title(&episode.series_name),
        episode.season,
        episode.episode,
        episode.title
    ));
    let strm = season_folder.join(format!("{title}.strm"));
    desired_paths.insert(strm.clone());
    if written_paths.insert(strm.clone()) {
        count_status(
            write_text(&strm, &item.url, output.dry_run, output.incremental).await?,
            true,
            summary,
        );
    }
    if output.generate_nfo {
        let nfo = strm.with_extension("nfo");
        desired_paths.insert(nfo.clone());
        count_status(
            write_text(&nfo, &episode_nfo(item), output.dry_run, output.incremental).await?,
            false,
            summary,
        );
    }
    Ok(())
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum WriteStatus {
    Written,
    Unchanged,
    DryRun,
}

async fn write_text(
    path: &Path,
    content: &str,
    dry_run: bool,
    incremental: bool,
) -> Result<WriteStatus> {
    if dry_run {
        return Ok(WriteStatus::DryRun);
    }
    if incremental {
        if let Ok(existing) = fs::read_to_string(path).await {
            if existing == content {
                return Ok(WriteStatus::Unchanged);
            }
        }
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create {}", parent.display()))?;
    }
    fs::write(path, content)
        .await
        .with_context(|| format!("write {}", path.display()))?;
    Ok(WriteStatus::Written)
}

fn count_status(status: WriteStatus, strm: bool, summary: &mut WriteSummary) {
    match status {
        WriteStatus::Written if strm => summary.strm_files_written += 1,
        WriteStatus::Written => summary.nfo_files_written += 1,
        WriteStatus::Unchanged => summary.unchanged_files += 1,
        WriteStatus::DryRun => {}
    }
}

fn movie_nfo(item: &MediaItem) -> String {
    let mut fields = BTreeMap::new();
    fields.insert("title", clean_title(&item.name));
    if let Some(year) = item.year {
        fields.insert("year", year.to_string());
    }
    if let Some(tmdb) = item.tmdb_id.as_ref() {
        fields.insert("tmdbid", tmdb.clone());
    }
    xml("movie", fields)
}

fn series_nfo(item: &MediaItem) -> String {
    let title = item
        .episode
        .as_ref()
        .map(|ep| ep.series_name.as_str())
        .unwrap_or(&item.name);
    let mut fields = BTreeMap::new();
    fields.insert("title", clean_title(title));
    if let Some(year) = item.year {
        fields.insert("year", year.to_string());
    }
    if let Some(tmdb) = item.tmdb_id.as_ref() {
        fields.insert("tmdbid", tmdb.clone());
    }
    xml("tvshow", fields)
}

fn episode_nfo(item: &MediaItem) -> String {
    let Some(ep) = item.episode.as_ref() else {
        return String::new();
    };
    let mut fields = BTreeMap::new();
    fields.insert("title", ep.title.clone());
    fields.insert("season", ep.season.to_string());
    fields.insert("episode", ep.episode.to_string());
    xml("episodedetails", fields)
}

fn xml(root: &str, fields: BTreeMap<&str, String>) -> String {
    let mut out = format!("<{root}>\n");
    for (key, value) in fields {
        out.push_str(&format!("  <{key}>{}</{key}>\n", escape_xml(&value)));
    }
    out.push_str(&format!("</{root}>\n"));
    out
}

fn escape_attr(value: &str) -> String {
    value.replace('&', "&amp;").replace('"', "&quot;")
}

fn escape_xml(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn cleanup_tree(root: &Path, desired_paths: &HashSet<PathBuf>) -> Result<()> {
    if !root.exists() {
        return Ok(());
    }
    cleanup_files(root, desired_paths)?;
    prune_empty_dirs(root)?;
    Ok(())
}

fn cleanup_files(dir: &Path, desired_paths: &HashSet<PathBuf>) -> Result<()> {
    for entry in std::fs::read_dir(dir).with_context(|| format!("read {}", dir.display()))? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            cleanup_files(&path, desired_paths)?;
        } else if matches!(
            path.extension().and_then(|ext| ext.to_str()),
            Some("strm" | "nfo")
        ) && !desired_paths.contains(&path)
        {
            std::fs::remove_file(&path)
                .with_context(|| format!("remove stale {}", path.display()))?;
        }
    }
    Ok(())
}

fn prune_empty_dirs(dir: &Path) -> Result<bool> {
    let mut empty = true;
    for entry in std::fs::read_dir(dir).with_context(|| format!("read {}", dir.display()))? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            if prune_empty_dirs(&path)? {
                std::fs::remove_dir(&path)
                    .with_context(|| format!("remove empty {}", path.display()))?;
            } else {
                empty = false;
            }
        } else {
            empty = false;
        }
    }
    Ok(empty)
}
