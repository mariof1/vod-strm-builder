use anyhow::Result;
use regex::Regex;

use crate::model::{MediaItem, MediaKind};

pub fn matches_target(item: &MediaItem, filter: Option<&str>, groups: &[String]) -> Result<bool> {
    if !groups.is_empty() && !groups.iter().any(|group| group == &item.group) {
        return Ok(false);
    }
    matches_filter(item, filter)
}

pub fn matches_filter(item: &MediaItem, filter: Option<&str>) -> Result<bool> {
    let Some(filter) = filter.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(true);
    };
    if filter.eq_ignore_ascii_case("all") || filter == r#"Group ~ ".*""# {
        return Ok(true);
    }
    for part in split_and(filter) {
        if !matches_atom(item, part.trim())? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn split_and(filter: &str) -> Vec<&str> {
    filter.split(" AND ").collect()
}

fn matches_atom(item: &MediaItem, atom: &str) -> Result<bool> {
    let atom = atom.trim().trim_matches(|ch| ch == '(' || ch == ')').trim();
    if let Some(value) = atom.strip_prefix("Type =") {
        return Ok(match value.trim().to_ascii_lowercase().as_str() {
            "live" => item.kind == MediaKind::Live,
            "vod" | "movie" => item.kind == MediaKind::Movie,
            "series" => item.kind == MediaKind::Series,
            _ => false,
        });
    }
    let re = Regex::new(r#"^(Group|Title|Name|Input|Url|Type)\s*~\s*"(.+)"$"#)?;
    if let Some(caps) = re.captures(atom) {
        let field = caps.get(1).map(|value| value.as_str()).unwrap_or_default();
        let pattern = caps.get(2).map(|value| value.as_str()).unwrap_or_default();
        let re = Regex::new(pattern)?;
        let value = match field {
            "Group" => item.group.as_str(),
            "Title" | "Name" => item.name.as_str(),
            "Input" => item.input.as_str(),
            "Url" => item.url.as_str(),
            "Type" => match item.kind {
                MediaKind::Live => "live",
                MediaKind::Movie => "movie",
                MediaKind::Series => "series",
            },
            _ => "",
        };
        return Ok(re.is_match(value));
    }
    anyhow::bail!("unsupported filter expression: {atom}");
}
