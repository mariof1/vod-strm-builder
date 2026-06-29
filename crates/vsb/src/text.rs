use regex::Regex;

pub fn clean_title(name: &str) -> String {
    let mut text = name.trim().replace(['–', '—'], "-");
    text = Regex::new(r"(?i)^(?:\|?[A-Z]{2,4}\|?|\[[A-Z]{2,4}\]|\([A-Z]{2,4}\))\s*[-|]\s*")
        .expect("valid regex")
        .replace_all(&text, "")
        .to_string();
    text = Regex::new(r"(?i)^(?:NF|A\+|D\+|AMZN|DSNP|ATVP|HBO|MAX|MULTI)\s*[-:|]\s*")
        .expect("valid regex")
        .replace_all(&text, "")
        .to_string();
    if let Some(mat) = Regex::new(r"\((19\d{2}|20\d{2}|21\d{2})\)")
        .expect("valid regex")
        .find(&text)
    {
        text.truncate(mat.end());
    }
    text =
        Regex::new(r"(?i)\b(4k|8k|uhd|fhd|hd|sd|hdr|web[- .]?dl|webrip|bluray|x264|x265|hevc)\b")
            .expect("valid regex")
            .replace_all(&text, " ")
            .to_string();
    Regex::new(r"\s+")
        .expect("valid regex")
        .replace_all(text.trim_matches(&[' ', '-', '.', '_'][..]), " ")
        .trim()
        .to_string()
}

pub fn extract_year(name: &str) -> Option<u16> {
    Regex::new(r"(19\d{2}|20\d{2}|21\d{2})")
        .expect("valid regex")
        .captures(name)
        .and_then(|caps| caps.get(1))
        .and_then(|year| year.as_str().parse::<u16>().ok())
}

pub fn safe_filename(name: &str) -> String {
    let replaced = name.replace(':', " - ").replace(['/', '\\', '|'], "-");
    let cleaned = Regex::new(r#"[<>:"?*\x00-\x1f]"#)
        .expect("valid regex")
        .replace_all(&replaced, "")
        .to_string();
    let compact = Regex::new(r"\s+")
        .expect("valid regex")
        .replace_all(cleaned.trim().trim_end_matches(['.', ' ']), " ")
        .to_string();
    let value = if compact.is_empty() {
        "Unknown".to_string()
    } else {
        compact
    };
    value
        .chars()
        .take(180)
        .collect::<String>()
        .trim_end_matches(['.', ' '])
        .to_string()
}

pub fn strip_redundant_year(name: &str, year: Option<u16>) -> String {
    if let Some(year) = year {
        let suffix = format!(" ({year})");
        if let Some(stripped) = name.strip_suffix(&suffix) {
            return stripped.trim().to_string();
        }
    }
    name.trim().to_string()
}

pub fn folder_name(
    name: &str,
    year: Option<u16>,
    tmdb_id: Option<&str>,
    append_tmdb: bool,
) -> String {
    let title = strip_redundant_year(&clean_title(name), year);
    let mut base = if let Some(year) = year {
        format!("{} ({year})", safe_filename(&title))
    } else {
        safe_filename(&title)
    };
    if append_tmdb {
        if let Some(tmdb_id) = tmdb_id.filter(|value| !value.trim().is_empty()) {
            base.push_str(&format!(" {{tmdb-{}}}", tmdb_id.trim()));
        }
    }
    base
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleans_provider_prefixes() {
        assert_eq!(
            folder_name(
                "NF - Dune Part Two 4K (2024)",
                Some(2024),
                Some("693134"),
                true
            ),
            "Dune Part Two (2024) {tmdb-693134}"
        );
    }
}
