use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Eq, PartialEq, Hash)]
#[serde(rename_all = "lowercase")]
pub enum MediaKind {
    Live,
    Movie,
    Series,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MediaItem {
    pub input: String,
    pub kind: MediaKind,
    pub group: String,
    pub name: String,
    pub url: String,
    pub stream_id: Option<String>,
    pub extension: Option<String>,
    pub year: Option<u16>,
    pub tmdb_id: Option<String>,
    pub logo: Option<String>,
    pub episode: Option<EpisodeInfo>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EpisodeInfo {
    pub series_name: String,
    pub season: u16,
    pub episode: u16,
    pub title: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ScanSummary {
    pub inputs: Vec<InputSummary>,
    pub total_live: usize,
    pub total_movies: usize,
    pub total_series_episodes: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputSummary {
    pub name: String,
    pub live: usize,
    pub movies: usize,
    pub series_episodes: usize,
    pub groups: Vec<GroupSummary>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupSummary {
    pub name: String,
    pub live: usize,
    pub movies: usize,
    pub series_episodes: usize,
}
