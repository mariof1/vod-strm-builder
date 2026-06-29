mod config;
mod filter;
mod jellyfin;
mod m3u;
mod model;
mod pipeline;
mod server;
mod text;
mod tmdb;
mod writer;
mod xtream;

use std::{net::SocketAddr, path::PathBuf};

use anyhow::Result;
use clap::{Parser, Subcommand};

use crate::config::SourceConfig;

#[derive(Debug, Parser)]
#[command(author, version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Generate configured target outputs from source.yml.
    Generate {
        /// Tuliprox-style source config.
        #[arg(short, long, default_value = "config/source.yml")]
        source: PathBuf,
        /// Only process one target name.
        #[arg(short, long)]
        target: Option<String>,
        /// Print summary JSON to stdout.
        #[arg(long)]
        json: bool,
    },
    /// Fetch inputs and print group/category counts without writing outputs.
    Scan {
        /// Tuliprox-style source config.
        #[arg(short, long, default_value = "config/source.yml")]
        source: PathBuf,
        /// Print JSON instead of text.
        #[arg(long)]
        json: bool,
    },
    /// Print an example source.yml.
    Example,
    /// Run the Rust web UI and API.
    Serve {
        /// Address to bind, for example 0.0.0.0:8080.
        #[arg(long, env = "VSB_BIND", default_value = "0.0.0.0:8080")]
        bind: SocketAddr,
        /// Persistent work directory containing source.yml.
        #[arg(long, env = "VSB_WORK_DIR", default_value = "/work")]
        work_dir: PathBuf,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Generate {
            source,
            target,
            json,
        } => {
            let config = SourceConfig::from_file(&source).await?;
            let summary = pipeline::generate(&config, target.as_deref()).await?;
            if json {
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                println!("{summary}");
            }
        }
        Command::Scan { source, json } => {
            let config = SourceConfig::from_file(&source).await?;
            let summary = pipeline::scan(&config).await?;
            if json {
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                println!("{summary}");
            }
        }
        Command::Example => {
            println!(
                "{}",
                include_str!("../../../examples/source.rust.example.yml")
            );
        }
        Command::Serve { bind, work_dir } => {
            server::serve(bind, work_dir).await?;
        }
    }
    Ok(())
}
