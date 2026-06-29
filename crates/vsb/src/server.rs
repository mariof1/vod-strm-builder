use std::{
    collections::HashMap,
    io::ErrorKind,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result};
use axum::{
    extract::{Path as AxumPath, State},
    http::StatusCode,
    response::{Html, IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::{fs, sync::Mutex, task::JoinHandle, time};
use tower_http::trace::TraceLayer;

use crate::{config::SourceConfig, model::ScanSummary, pipeline};

const INDEX_HTML: &str = include_str!("../../../web/index.html");
const EXAMPLE_SOURCE: &str = include_str!("../../../examples/source.rust.example.yml");

#[derive(Clone)]
struct AppState {
    work_dir: Arc<PathBuf>,
    jobs: Arc<Mutex<HashMap<String, JobRecord>>>,
    handles: Arc<Mutex<HashMap<String, JoinHandle<()>>>>,
    counter: Arc<AtomicU64>,
}

impl AppState {
    fn new(work_dir: PathBuf) -> Self {
        Self {
            work_dir: Arc::new(work_dir),
            jobs: Arc::new(Mutex::new(HashMap::new())),
            handles: Arc::new(Mutex::new(HashMap::new())),
            counter: Arc::new(AtomicU64::new(1)),
        }
    }

    fn source_path(&self) -> PathBuf {
        self.work_dir.join("source.yml")
    }

    fn groups_path(&self) -> PathBuf {
        self.work_dir.join("groups.json")
    }

    fn settings_path(&self) -> PathBuf {
        self.work_dir.join("app-settings.json")
    }

    fn scheduler_state_path(&self) -> PathBuf {
        self.work_dir.join("scheduler-state.json")
    }

    fn last_run_path(&self) -> PathBuf {
        self.work_dir.join("last-run.json")
    }
}

pub async fn serve(bind: SocketAddr, work_dir: PathBuf) -> Result<()> {
    fs::create_dir_all(&work_dir)
        .await
        .with_context(|| format!("create work dir {}", work_dir.display()))?;
    let state = AppState::new(work_dir);
    tokio::spawn(scheduler_loop(state.clone()));
    let app = Router::new()
        .route("/", get(index))
        .route("/api/health", get(health))
        .route("/api/example", get(example))
        .route("/api/source", get(get_source).post(save_source))
        .route("/api/config", get(get_config).post(save_config))
        .route("/api/settings", get(get_settings).post(save_settings))
        .route("/api/groups", get(get_groups))
        .route("/api/scan", post(start_scan))
        .route("/api/generate", post(start_generate))
        .route("/api/jobs", get(list_jobs).post(start_job_endpoint))
        .route("/api/jobs/{id}", get(get_job))
        .route("/api/jobs/{id}/log", get(get_job_log))
        .route("/api/jobs/{id}/cancel", post(cancel_job))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    tracing::info!(%bind, "starting rust web server");
    let listener = tokio::net::TcpListener::bind(bind).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        version: env!("CARGO_PKG_VERSION"),
    })
}

async fn example() -> Json<SourceResponse> {
    Json(SourceResponse {
        source: EXAMPLE_SOURCE.to_string(),
        path: "example".to_string(),
    })
}

async fn get_source(State(state): State<AppState>) -> ApiResult<Json<SourceResponse>> {
    let path = state.source_path();
    let source = read_source_or_example(&path).await?;
    Ok(Json(SourceResponse {
        source,
        path: path.display().to_string(),
    }))
}

async fn save_source(
    State(state): State<AppState>,
    Json(body): Json<SourcePayload>,
) -> ApiResult<Json<SourceResponse>> {
    let source = body.source.unwrap_or_default();
    SourceConfig::from_str(&source, "request body")?;
    let path = state.source_path();
    write_text(&path, &source).await?;
    tracing::info!(path = %path.display(), "saved source config");
    Ok(Json(SourceResponse {
        source,
        path: path.display().to_string(),
    }))
}

async fn get_config(State(state): State<AppState>) -> ApiResult<Json<ConfigResponse>> {
    let path = state.source_path();
    let source = read_source_or_example(&path).await?;
    let config = SourceConfig::from_str(&source, &path.display().to_string())?;
    Ok(Json(ConfigResponse {
        config,
        source,
        path: path.display().to_string(),
    }))
}

async fn save_config(
    State(state): State<AppState>,
    Json(config): Json<SourceConfig>,
) -> ApiResult<Json<ConfigResponse>> {
    config.validate_config()?;
    let source = config.to_yaml()?;
    let path = state.source_path();
    write_text(&path, &source).await?;
    Ok(Json(ConfigResponse {
        config,
        source,
        path: path.display().to_string(),
    }))
}

async fn get_settings(State(state): State<AppState>) -> ApiResult<Json<RuntimeSettings>> {
    Ok(Json(read_settings(&state).await?))
}

async fn save_settings(
    State(state): State<AppState>,
    Json(settings): Json<RuntimeSettings>,
) -> ApiResult<Json<RuntimeSettings>> {
    write_json(&state.settings_path(), &settings).await?;
    Ok(Json(settings))
}

async fn get_groups(State(state): State<AppState>) -> ApiResult<Json<GroupsResponse>> {
    match read_json::<GroupsResponse>(&state.groups_path()).await {
        Ok(groups) => Ok(Json(groups)),
        Err(error) if is_not_found(&error) => Ok(Json(GroupsResponse::default())),
        Err(error) => Err(error.into()),
    }
}

async fn start_scan(
    State(state): State<AppState>,
    Json(body): Json<SourcePayload>,
) -> ApiResult<Json<JobStartResponse>> {
    let record = start_job(state, JobKind::Sync, body.source, body.save, None).await?;
    Ok(Json(JobStartResponse { job: record }))
}

async fn start_generate(
    State(state): State<AppState>,
    Json(body): Json<GeneratePayload>,
) -> ApiResult<Json<JobStartResponse>> {
    let record = start_job(
        state,
        JobKind::Generate,
        body.source,
        body.save,
        body.target,
    )
    .await?;
    Ok(Json(JobStartResponse { job: record }))
}

async fn start_job_endpoint(
    State(state): State<AppState>,
    Json(body): Json<JobRequest>,
) -> ApiResult<Json<JobStartResponse>> {
    let record = start_job(state, body.action, body.source, body.save, body.target).await?;
    Ok(Json(JobStartResponse { job: record }))
}

async fn list_jobs(State(state): State<AppState>) -> Json<Vec<JobRecord>> {
    let mut jobs = state
        .jobs
        .lock()
        .await
        .values()
        .cloned()
        .collect::<Vec<_>>();
    jobs.sort_by(|a, b| b.started_at.cmp(&a.started_at));
    Json(jobs)
}

async fn get_job(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
) -> ApiResult<Json<JobRecord>> {
    let jobs = state.jobs.lock().await;
    let job = jobs
        .get(&id)
        .cloned()
        .with_context(|| format!("unknown job {id}"))?;
    Ok(Json(job))
}

async fn get_job_log(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
) -> ApiResult<String> {
    let jobs = state.jobs.lock().await;
    let job = jobs.get(&id).with_context(|| format!("unknown job {id}"))?;
    Ok(job.log.join("\n"))
}

async fn cancel_job(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
) -> ApiResult<Json<JobRecord>> {
    if let Some(handle) = state.handles.lock().await.remove(&id) {
        handle.abort();
    }
    update_job(&state, &id, |job| {
        job.status = JobStatus::Canceled;
        job.progress = 100;
        job.message = "Canceled".to_string();
        job.finished_at = Some(now_epoch());
        job.log.push(format!("{} canceled", now_epoch()));
    })
    .await?;
    get_job(State(state), AxumPath(id)).await
}

async fn start_job(
    state: AppState,
    kind: JobKind,
    source: Option<String>,
    save: Option<bool>,
    target: Option<String>,
) -> Result<JobRecord> {
    let id = format!("job-{}", state.counter.fetch_add(1, Ordering::Relaxed));
    let record = JobRecord {
        id: id.clone(),
        kind,
        status: JobStatus::Queued,
        progress: 0,
        message: "Queued".to_string(),
        started_at: now_epoch(),
        finished_at: None,
        log: vec![format!("{} queued {kind:?}", now_epoch())],
        result: None,
        error: None,
    };
    state.jobs.lock().await.insert(id.clone(), record.clone());
    let task_state = state.clone();
    let task_id = id.clone();
    let handle = tokio::spawn(async move {
        run_job(
            task_state.clone(),
            task_id.clone(),
            kind,
            source,
            save,
            target,
        )
        .await;
        task_state.handles.lock().await.remove(&task_id);
    });
    state.handles.lock().await.insert(id, handle);
    Ok(record)
}

async fn run_job(
    state: AppState,
    id: String,
    kind: JobKind,
    source: Option<String>,
    save: Option<bool>,
    target: Option<String>,
) {
    let result: Result<Value> = async {
        set_job(&state, &id, JobStatus::Running, 2, "Preparing").await?;
        let source_path = state.source_path();
        let source = if let Some(source) = source.filter(|value| !value.trim().is_empty()) {
            source
        } else {
            read_source_or_example(&source_path).await?
        };
        if save.unwrap_or(false) {
            write_text(&source_path, &source).await?;
            append_job_log(&state, &id, format!("saved {}", source_path.display())).await?;
        }
        set_job(&state, &id, JobStatus::Running, 8, "Parsing source").await?;
        let config = SourceConfig::from_str(&source, "job source")?;
        match kind {
            JobKind::Sync => {
                set_job(&state, &id, JobStatus::Running, 15, "Scanning playlists").await?;
                let summary = pipeline::scan(&config).await?;
                set_job(&state, &id, JobStatus::Running, 92, "Saving group cache").await?;
                let groups = GroupsResponse {
                    updated_at: Some(now_epoch()),
                    summary: Some(summary.clone()),
                };
                write_json(&state.groups_path(), &groups).await?;
                Ok(serde_json::to_value(groups)?)
            }
            JobKind::Generate => {
                set_job(&state, &id, JobStatus::Running, 12, "Generating library").await?;
                let summary =
                    pipeline::generate_with_work_dir(&config, target.as_deref(), &state.work_dir)
                        .await?;
                set_job(&state, &id, JobStatus::Running, 94, "Saving summaries").await?;
                let groups = GroupsResponse {
                    updated_at: Some(now_epoch()),
                    summary: Some(summary.scanned.clone()),
                };
                write_json(&state.groups_path(), &groups).await?;
                write_json(&state.last_run_path(), &summary).await?;
                Ok(serde_json::to_value(summary)?)
            }
        }
    }
    .await;
    match result {
        Ok(value) => {
            update_job(&state, &id, |job| {
                job.status = JobStatus::Complete;
                job.progress = 100;
                job.message = "Complete".to_string();
                job.finished_at = Some(now_epoch());
                job.result = Some(value);
                job.log.push(format!("{} complete", now_epoch()));
            })
            .await
            .ok();
        }
        Err(error) => {
            let error = error.to_string();
            update_job(&state, &id, |job| {
                job.status = JobStatus::Failed;
                job.progress = 100;
                job.message = "Failed".to_string();
                job.finished_at = Some(now_epoch());
                job.error = Some(error.clone());
                job.log.push(format!("{} failed: {error}", now_epoch()));
            })
            .await
            .ok();
        }
    }
}

async fn scheduler_loop(state: AppState) {
    let mut ticker = time::interval(Duration::from_secs(60));
    loop {
        ticker.tick().await;
        if let Err(error) = scheduler_tick(&state).await {
            tracing::warn!(error = %error, "scheduler tick failed");
        }
    }
}

async fn scheduler_tick(state: &AppState) -> Result<()> {
    let settings = read_settings(state).await?;
    if !settings.scheduler.enabled {
        return Ok(());
    }
    if has_running_job(state).await {
        return Ok(());
    }
    let now = now_epoch();
    let mut scheduler_state = read_json::<SchedulerState>(&state.scheduler_state_path())
        .await
        .unwrap_or_default();
    if scheduler_state.last_run_at.is_none() && !settings.scheduler.run_on_start {
        scheduler_state.last_run_at = Some(now);
        write_json(&state.scheduler_state_path(), &scheduler_state).await?;
        return Ok(());
    }
    let interval = settings.scheduler.interval_minutes.max(1) * 60;
    let due = scheduler_state
        .last_run_at
        .map(|last| now.saturating_sub(last) >= interval)
        .unwrap_or(true);
    if due {
        start_job(
            state.clone(),
            settings.scheduler.action,
            None,
            Some(false),
            settings.scheduler.target.clone(),
        )
        .await?;
        scheduler_state.last_run_at = Some(now);
        write_json(&state.scheduler_state_path(), &scheduler_state).await?;
    }
    Ok(())
}

async fn has_running_job(state: &AppState) -> bool {
    state
        .jobs
        .lock()
        .await
        .values()
        .any(|job| matches!(job.status, JobStatus::Queued | JobStatus::Running))
}

async fn set_job(
    state: &AppState,
    id: &str,
    status: JobStatus,
    progress: u8,
    message: &str,
) -> Result<()> {
    update_job(state, id, |job| {
        job.status = status;
        job.progress = progress.min(100);
        job.message = message.to_string();
        job.log.push(format!("{} {message}", now_epoch()));
    })
    .await
}

async fn append_job_log(state: &AppState, id: &str, line: String) -> Result<()> {
    update_job(state, id, |job| {
        job.log.push(format!("{} {line}", now_epoch()));
    })
    .await
}

async fn update_job(state: &AppState, id: &str, update: impl FnOnce(&mut JobRecord)) -> Result<()> {
    let mut jobs = state.jobs.lock().await;
    let job = jobs
        .get_mut(id)
        .with_context(|| format!("unknown job {id}"))?;
    update(job);
    Ok(())
}

async fn read_settings(state: &AppState) -> Result<RuntimeSettings> {
    match read_json(&state.settings_path()).await {
        Ok(settings) => Ok(settings),
        Err(error) if is_not_found(&error) => Ok(RuntimeSettings::default()),
        Err(error) => Err(error),
    }
}

async fn read_source_or_example(path: &Path) -> Result<String> {
    match fs::read_to_string(path).await {
        Ok(source) => Ok(source),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(EXAMPLE_SOURCE.to_string()),
        Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
    }
}

async fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let raw = fs::read_to_string(path)
        .await
        .with_context(|| format!("read {}", path.display()))?;
    serde_json::from_str(&raw).with_context(|| format!("parse {}", path.display()))
}

async fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    write_text(path, &serde_json::to_string_pretty(value)?).await
}

async fn write_text(path: &Path, content: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create {}", parent.display()))?;
    }
    fs::write(path, content)
        .await
        .with_context(|| format!("write {}", path.display()))
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn is_not_found(error: &anyhow::Error) -> bool {
    error
        .chain()
        .find_map(|error| error.downcast_ref::<std::io::Error>())
        .is_some_and(|error| error.kind() == ErrorKind::NotFound)
}

type ApiResult<T> = std::result::Result<T, ApiError>;

struct ApiError(anyhow::Error);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(ErrorResponse {
            error: self.0.to_string(),
        });
        (StatusCode::BAD_REQUEST, body).into_response()
    }
}

impl From<anyhow::Error> for ApiError {
    fn from(error: anyhow::Error) -> Self {
        Self(error)
    }
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    status: &'static str,
    version: &'static str,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    error: String,
}

#[derive(Debug, Serialize)]
struct SourceResponse {
    source: String,
    path: String,
}

#[derive(Debug, Serialize)]
struct ConfigResponse {
    config: SourceConfig,
    source: String,
    path: String,
}

#[derive(Debug, Deserialize)]
struct SourcePayload {
    source: Option<String>,
    save: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct GeneratePayload {
    source: Option<String>,
    save: Option<bool>,
    target: Option<String>,
}

#[derive(Debug, Deserialize)]
struct JobRequest {
    action: JobKind,
    source: Option<String>,
    save: Option<bool>,
    target: Option<String>,
}

#[derive(Debug, Serialize)]
struct JobStartResponse {
    job: JobRecord,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum JobKind {
    Sync,
    Generate,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum JobStatus {
    Queued,
    Running,
    Complete,
    Failed,
    Canceled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JobRecord {
    id: String,
    kind: JobKind,
    status: JobStatus,
    progress: u8,
    message: String,
    started_at: u64,
    finished_at: Option<u64>,
    log: Vec<String>,
    result: Option<Value>,
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct RuntimeSettings {
    scheduler: SchedulerSettings,
}

impl Default for RuntimeSettings {
    fn default() -> Self {
        Self {
            scheduler: SchedulerSettings::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct SchedulerSettings {
    enabled: bool,
    interval_minutes: u64,
    action: JobKind,
    target: Option<String>,
    run_on_start: bool,
}

impl Default for SchedulerSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            interval_minutes: 1440,
            action: JobKind::Generate,
            target: None,
            run_on_start: false,
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct SchedulerState {
    last_run_at: Option<u64>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct GroupsResponse {
    updated_at: Option<u64>,
    summary: Option<ScanSummary>,
}
