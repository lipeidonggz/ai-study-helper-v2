//! Tauri 桌面壳：管理 Python sidecar、启动令牌、有限重启和进程树清理。

#![warn(missing_docs)]

use serde::Serialize;
use std::{
    fs::{self, OpenOptions},
    mem::size_of,
    net::TcpListener,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager, State};
use uuid::Uuid;

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(feature = "eval-console")]
use std::path::Path;
#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE},
    System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    },
};

const MAX_RESTART_ATTEMPTS: u32 = 2;
const HEALTHY_RESTART_RESET: Duration = Duration::from_secs(30);
const APP_DATA_DIRECTORIES: &[&str] = &[
    "config",
    "db",
    "collections",
    "prompts/experiments",
    "traces",
    "logs",
    "backups",
];

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendConnection {
    base_url: String,
    token: String,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendStatus {
    status: &'static str,
    port: u16,
    restart_count: u32,
    message: Option<String>,
}

#[derive(Clone)]
struct BackendLaunchSpec {
    executable: PathBuf,
    working_directory: PathBuf,
    arguments: Vec<String>,
    log_directory: PathBuf,
}

/// 管理 Python sidecar 的生命周期、重启上限和 Windows 进程树清理。
struct BackendManager {
    port: u16,
    token: String,
    job_handle: usize,
    launch_spec: Mutex<Option<BackendLaunchSpec>>,
    data_root: Mutex<Option<PathBuf>>,
    child: Mutex<Option<Child>>,
    shutting_down: AtomicBool,
    restart_count: AtomicU32,
    started_at: Mutex<Option<Instant>>,
    last_status: Mutex<BackendStatus>,
}

impl BackendManager {
    /// 创建只绑定回环地址的后端管理器。
    fn new(port: u16, token: String) -> Result<Self, String> {
        Ok(Self {
            port,
            token,
            job_handle: create_job_object()?,
            launch_spec: Mutex::new(None),
            data_root: Mutex::new(None),
            child: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            restart_count: AtomicU32::new(0),
            started_at: Mutex::new(None),
            last_status: Mutex::new(BackendStatus {
                status: "starting",
                port,
                restart_count: 0,
                message: None,
            }),
        })
    }

    /// 返回当前前端可用的回环地址和启动令牌。
    fn connection(&self) -> BackendConnection {
        BackendConnection {
            base_url: format!("http://127.0.0.1:{}", self.port),
            token: self.token.clone(),
        }
    }

    /// 返回最近一次 sidecar 状态快照。
    fn status(&self) -> BackendStatus {
        self.last_status
            .lock()
            .expect("backend status lock poisoned")
            .clone()
    }

    /// 更新状态并向前端发送事件。
    fn set_status(&self, app: &AppHandle, status: &'static str, message: Option<String>) {
        let next = BackendStatus {
            status,
            port: self.port,
            restart_count: self.restart_count.load(Ordering::SeqCst),
            message,
        };
        *self
            .last_status
            .lock()
            .expect("backend status lock poisoned") = next.clone();
        let _ = app.emit("backend-status", next);
    }

    /// 启动 sidecar，并通过环境变量传递启动令牌和数据根。
    fn spawn_child(&self) -> Result<Child, String> {
        let launch_spec = self
            .launch_spec
            .lock()
            .expect("backend launch spec lock poisoned")
            .clone()
            .ok_or_else(|| "backend launch specification is unavailable".to_owned())?;
        let data_root = self
            .data_root
            .lock()
            .expect("backend data root lock poisoned")
            .clone()
            .ok_or_else(|| "application data root is unavailable".to_owned())?;
        std::fs::create_dir_all(&launch_spec.log_directory)
            .map_err(|error| format!("failed to create sidecar log directory: {error}"))?;
        let log_path = launch_spec.log_directory.join("sidecar.log");
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| format!("failed to open sidecar log: {error}"))?;
        let error_log = log
            .try_clone()
            .map_err(|error| format!("failed to clone sidecar log: {error}"))?;

        let mut command = Command::new(&launch_spec.executable);
        command
            .current_dir(&launch_spec.working_directory)
            .args(&launch_spec.arguments)
            .arg("--port")
            .arg(self.port.to_string())
            .env("ASH_LAUNCH_TOKEN", &self.token)
            .env("ASH_DATA_ROOT", data_root)
            .env(
                "ASH_TRACE_MODE",
                if cfg!(debug_assertions) {
                    "full"
                } else {
                    "metadata"
                },
            )
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(error_log));

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to start Python sidecar: {error}"))?;

        #[cfg(windows)]
        {
            let assigned = unsafe {
                AssignProcessToJobObject(self.job_handle as HANDLE, child.as_raw_handle() as HANDLE)
            };
            if assigned == 0 {
                let error = std::io::Error::last_os_error();
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("failed to attach sidecar to job object: {error}"));
            }
        }

        Ok(child)
    }

    /// 启动 sidecar 并进入健康检查与有限重启流程。
    fn start(self: &Arc<Self>, app: AppHandle, data_root: PathBuf) {
        self.set_status(&app, "starting", None);
        *self
            .data_root
            .lock()
            .expect("backend data root lock poisoned") = Some(data_root);
        match resolve_launch_spec(&app) {
            Ok(launch_spec) => {
                *self
                    .launch_spec
                    .lock()
                    .expect("backend launch spec lock poisoned") = Some(launch_spec);
            }
            Err(error) => {
                self.set_status(&app, "failed", Some(error));
                self.start_monitor(app);
                return;
            }
        }
        match self.spawn_child() {
            Ok(child) => {
                *self.child.lock().expect("backend child lock poisoned") = Some(child);
                *self
                    .started_at
                    .lock()
                    .expect("backend start time lock poisoned") = Some(Instant::now());
                self.set_status(
                    &app,
                    "running",
                    Some("Python sidecar 已启动，等待健康检查。".to_owned()),
                );
            }
            Err(error) => {
                self.set_status(&app, "failed", Some(error));
            }
        }
        self.start_monitor(app);
    }

    /// 监控 sidecar 退出，区分配置内停止和意外退出。
    fn start_monitor(self: &Arc<Self>, app: AppHandle) {
        let manager = Arc::clone(self);
        thread::spawn(move || loop {
            thread::sleep(Duration::from_millis(500));
            if manager.shutting_down.load(Ordering::SeqCst) {
                break;
            }

            let exit_status = {
                let mut guard = manager.child.lock().expect("backend child lock poisoned");
                match guard.as_mut() {
                    Some(child) => match child.try_wait() {
                        Ok(Some(status)) => {
                            guard.take();
                            Some(Ok(status.to_string()))
                        }
                        Ok(None) => None,
                        Err(error) => {
                            let _ = child.kill();
                            guard.take();
                            Some(Err(error.to_string()))
                        }
                    },
                    None => None,
                }
            };

            if let Some(exit) = exit_status {
                if manager.shutting_down.load(Ordering::SeqCst) {
                    break;
                }

                let reason = match exit {
                    Ok(status) => format!("sidecar exited unexpectedly ({status})"),
                    Err(error) => format!("sidecar monitor failed: {error}"),
                };
                manager.restart_after_exit(&app, reason);
            } else if let Some(started_at) = *manager
                .started_at
                .lock()
                .expect("backend start time lock poisoned")
            {
                if started_at.elapsed() >= HEALTHY_RESTART_RESET {
                    manager.restart_count.store(0, Ordering::SeqCst);
                }
            }
        });
    }

    /// 在总重试上限内按退避策略重启 sidecar。
    fn restart_after_exit(&self, app: &AppHandle, reason: String) {
        self.terminate_job();
        let mut attempt = self.restart_count.load(Ordering::SeqCst);
        if attempt >= MAX_RESTART_ATTEMPTS {
            self.set_status(
                app,
                "failed",
                Some(format!(
                    "{reason}; 已达到自动重启上限 {MAX_RESTART_ATTEMPTS} 次。"
                )),
            );
            return;
        }
        while attempt < MAX_RESTART_ATTEMPTS {
            attempt += 1;
            self.restart_count.store(attempt, Ordering::SeqCst);
            self.set_status(
                app,
                "restarting",
                Some(format!("{reason}; 正在第 {attempt} 次重启。")),
            );
            thread::sleep(Duration::from_millis(750));
            if self.shutting_down.load(Ordering::SeqCst) {
                return;
            }

            match self.spawn_child() {
                Ok(child) => {
                    *self.child.lock().expect("backend child lock poisoned") = Some(child);
                    *self
                        .started_at
                        .lock()
                        .expect("backend start time lock poisoned") = Some(Instant::now());
                    self.set_status(
                        app,
                        "running",
                        Some(format!("sidecar 已自动重启（第 {attempt} 次）。")),
                    );
                    return;
                }
                Err(error) => {
                    if attempt == MAX_RESTART_ATTEMPTS {
                        self.set_status(
                            app,
                            "failed",
                            Some(format!("{reason}; 自动重启失败：{error}")),
                        );
                    }
                }
            }
        }
    }

    /// 优雅停止 sidecar，并在超时后强制终止进程树。
    fn shutdown(&self, app: &AppHandle) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }

        self.terminate_job();
        if let Some(mut child) = self
            .child
            .lock()
            .expect("backend child lock poisoned")
            .take()
        {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.set_status(
            app,
            "stopped",
            Some("桌面应用已退出，sidecar 已停止。".to_owned()),
        );
    }

    /// 终止 Windows Job Object 中的全部 sidecar 子进程。
    fn terminate_job(&self) {
        #[cfg(windows)]
        unsafe {
            let _ = TerminateJobObject(self.job_handle as HANDLE, 1);
        }
    }
}

#[cfg(feature = "eval-console")]
/// 按需管理独立评测 sidecar；评测进程和数据根均与产品侧隔离。
struct EvaluationBackendManager {
    port: u16,
    token: String,
    job_handle: usize,
    launch_spec: Mutex<Option<BackendLaunchSpec>>,
    roots: Mutex<Option<(PathBuf, PathBuf)>>,
    child: Mutex<Option<Child>>,
    started: AtomicBool,
    shutting_down: AtomicBool,
    restart_count: AtomicU32,
    last_status: Mutex<BackendStatus>,
}

#[cfg(feature = "eval-console")]
impl EvaluationBackendManager {
    /// 创建尚未启动的评测 sidecar 管理器。
    fn new(port: u16, token: String) -> Result<Self, String> {
        Ok(Self {
            port,
            token,
            job_handle: create_job_object()?,
            launch_spec: Mutex::new(None),
            roots: Mutex::new(None),
            child: Mutex::new(None),
            started: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
            restart_count: AtomicU32::new(0),
            last_status: Mutex::new(BackendStatus {
                status: "stopped",
                port,
                restart_count: 0,
                message: Some("评测 sidecar 尚未启动。".to_owned()),
            }),
        })
    }

    /// 返回评测前端使用的独立回环地址和启动令牌。
    fn connection(&self) -> BackendConnection {
        BackendConnection {
            base_url: format!("http://127.0.0.1:{}", self.port),
            token: self.token.clone(),
        }
    }

    /// 返回评测 sidecar 的最近状态。
    fn status(&self) -> BackendStatus {
        self.last_status
            .lock()
            .expect("evaluation backend status lock poisoned")
            .clone()
    }

    /// 保存产品 collection 根和独立评测数据根，首次访问时再启动进程。
    fn configure_roots(&self, product_root: PathBuf, eval_root: PathBuf) {
        *self
            .roots
            .lock()
            .expect("evaluation backend roots lock poisoned") = Some((product_root, eval_root));
    }

    /// 首次进入评测工作区时启动 sidecar。
    fn ensure_started(self: &Arc<Self>, app: &AppHandle) {
        if self.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let roots = self
            .roots
            .lock()
            .expect("evaluation backend roots lock poisoned")
            .clone();
        let Some((product_root, eval_root)) = roots else {
            self.set_status(app, "failed", Some("评测数据根尚未配置。".to_owned()));
            return;
        };
        self.start(app.clone(), product_root, eval_root);
    }

    /// 更新状态并向评测 UI 推送事件。
    fn set_status(&self, app: &AppHandle, status: &'static str, message: Option<String>) {
        let next = BackendStatus {
            status,
            port: self.port,
            restart_count: self.restart_count.load(Ordering::SeqCst),
            message,
        };
        *self
            .last_status
            .lock()
            .expect("evaluation backend status lock poisoned") = next.clone();
        let _ = app.emit("eval-backend-status", next);
    }

    /// 启动评测 Python 进程，并显式传入两个隔离的数据根。
    fn start(self: &Arc<Self>, app: AppHandle, product_root: PathBuf, eval_root: PathBuf) {
        self.set_status(&app, "starting", None);
        match resolve_eval_launch_spec(&app) {
            Ok(spec) => {
                *self
                    .launch_spec
                    .lock()
                    .expect("evaluation backend launch spec lock poisoned") = Some(spec);
            }
            Err(error) => {
                self.set_status(&app, "failed", Some(error));
                return;
            }
        }
        match self.spawn_child(&product_root, &eval_root) {
            Ok(child) => {
                *self
                    .child
                    .lock()
                    .expect("evaluation backend child lock poisoned") = Some(child);
                self.set_status(
                    &app,
                    "running",
                    Some("评测 sidecar 已启动，等待健康检查。".to_owned()),
                );
            }
            Err(error) => {
                self.set_status(&app, "failed", Some(error));
            }
        }
        self.start_monitor(app);
    }

    /// 使用独立启动令牌、端口、trace 级别和根目录启动子进程。
    fn spawn_child(&self, product_root: &Path, eval_root: &Path) -> Result<Child, String> {
        let launch_spec = self
            .launch_spec
            .lock()
            .expect("evaluation backend launch spec lock poisoned")
            .clone()
            .ok_or_else(|| "evaluation launch specification is unavailable".to_owned())?;
        std::fs::create_dir_all(&launch_spec.log_directory)
            .map_err(|error| format!("failed to create evaluation log directory: {error}"))?;
        let log_path = launch_spec.log_directory.join("evaluation-sidecar.log");
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| format!("failed to open evaluation sidecar log: {error}"))?;
        let error_log = log
            .try_clone()
            .map_err(|error| format!("failed to clone evaluation sidecar log: {error}"))?;

        let mut command = Command::new(&launch_spec.executable);
        command
            .current_dir(&launch_spec.working_directory)
            .args(&launch_spec.arguments)
            .arg("--port")
            .arg(self.port.to_string())
            .env("ASH_LAUNCH_TOKEN", &self.token)
            .env("ASH_DATA_ROOT", product_root)
            .env("ASH_EVAL_DATA_ROOT", eval_root)
            .env("ASH_TRACE_MODE", "full")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(error_log));

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to start evaluation sidecar: {error}"))?;
        #[cfg(windows)]
        {
            let assigned = unsafe {
                AssignProcessToJobObject(self.job_handle as HANDLE, child.as_raw_handle() as HANDLE)
            };
            if assigned == 0 {
                let error = std::io::Error::last_os_error();
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "failed to attach evaluation sidecar to job object: {error}"
                ));
            }
        }
        Ok(child)
    }

    /// 监控评测进程退出，并在有限次数内自动重启。
    fn start_monitor(self: &Arc<Self>, app: AppHandle) {
        let manager = Arc::clone(self);
        thread::spawn(move || loop {
            thread::sleep(Duration::from_millis(500));
            if manager.shutting_down.load(Ordering::SeqCst) {
                break;
            }
            let exit_status = {
                let mut guard = manager
                    .child
                    .lock()
                    .expect("evaluation backend child lock poisoned");
                match guard.as_mut() {
                    Some(child) => match child.try_wait() {
                        Ok(Some(status)) => {
                            guard.take();
                            Some(Ok(status.to_string()))
                        }
                        Ok(None) => None,
                        Err(error) => {
                            let _ = child.kill();
                            guard.take();
                            Some(Err(error.to_string()))
                        }
                    },
                    None => None,
                }
            };
            if let Some(exit) = exit_status {
                let reason = match exit {
                    Ok(status) => format!("evaluation sidecar exited unexpectedly ({status})"),
                    Err(error) => format!("evaluation sidecar monitor failed: {error}"),
                };
                manager.restart_after_exit(&app, reason);
            }
        });
    }

    /// 在独立重启上限内重新启动评测进程。
    fn restart_after_exit(&self, app: &AppHandle, reason: String) {
        self.terminate_job();
        let attempt = self.restart_count.fetch_add(1, Ordering::SeqCst) + 1;
        if attempt > MAX_RESTART_ATTEMPTS {
            self.set_status(
                app,
                "failed",
                Some(format!(
                    "{reason}; 已达到自动重启上限 {MAX_RESTART_ATTEMPTS} 次。"
                )),
            );
            return;
        }
        let roots = self
            .roots
            .lock()
            .expect("evaluation backend roots lock poisoned")
            .clone();
        let Some((product_root, eval_root)) = roots else {
            self.set_status(app, "failed", Some(reason));
            return;
        };
        self.set_status(
            app,
            "restarting",
            Some(format!("{reason}; 正在第 {attempt} 次重启。")),
        );
        thread::sleep(Duration::from_millis(750));
        match self.spawn_child(&product_root, &eval_root) {
            Ok(child) => {
                *self
                    .child
                    .lock()
                    .expect("evaluation backend child lock poisoned") = Some(child);
                self.set_status(
                    app,
                    "running",
                    Some(format!("评测 sidecar 已自动重启（第 {attempt} 次）。")),
                );
            }
            Err(error) => {
                self.set_status(
                    app,
                    "failed",
                    Some(format!("{reason}; 自动重启失败：{error}")),
                );
            }
        }
    }

    /// 停止评测进程树，不影响产品 sidecar。
    fn shutdown(&self, app: &AppHandle) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        self.terminate_job();
        if let Some(mut child) = self
            .child
            .lock()
            .expect("evaluation backend child lock poisoned")
            .take()
        {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.set_status(
            app,
            "stopped",
            Some("桌面应用已退出，评测 sidecar 已停止。".to_owned()),
        );
    }

    /// 终止独立 Job Object 中的评测 sidecar。
    fn terminate_job(&self) {
        #[cfg(windows)]
        unsafe {
            let _ = TerminateJobObject(self.job_handle as HANDLE, 1);
        }
    }
}

#[cfg(feature = "eval-console")]
impl Drop for EvaluationBackendManager {
    fn drop(&mut self) {
        self.terminate_job();
        #[cfg(windows)]
        unsafe {
            if self.job_handle != 0 {
                let _ = CloseHandle(self.job_handle as HANDLE);
            }
        }
    }
}

#[cfg(windows)]
/// 创建随桌面进程关闭而终止子进程的 Job Object。
fn create_job_object() -> Result<usize, String> {
    let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if handle.is_null() {
        return Err(format!(
            "failed to create sidecar job object: {}",
            std::io::Error::last_os_error()
        ));
    }

    let mut information: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    let configured = unsafe {
        SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            &information as *const _ as *const _,
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    if configured == 0 {
        let error = std::io::Error::last_os_error();
        unsafe {
            let _ = CloseHandle(handle);
        }
        return Err(format!("failed to configure sidecar job object: {error}"));
    }

    Ok(handle as usize)
}

#[cfg(not(windows))]
/// 非 Windows 环境下不使用 Job Object。
fn create_job_object() -> Result<usize, String> {
    Ok(0)
}

/// 创建 LocalAppData 下的产品数据目录和首次启动配置。
fn prepare_app_data_root(app: &AppHandle) -> Result<PathBuf, String> {
    let root = app
        .path()
        .app_local_data_dir()
        .map_err(|error| format!("failed to resolve application data directory: {error}"))?;
    for relative in APP_DATA_DIRECTORIES {
        fs::create_dir_all(root.join(relative))
            .map_err(|error| format!("failed to create application data directory: {error}"))?;
    }

    let config_path = root.join("config").join("app.json");
    if !config_path.exists() {
        fs::write(
            config_path,
            format!(
                "{{\n  \"schema_version\": 1,\n  \"created_by\": \"{}\"\n}}\n",
                env!("CARGO_PKG_VERSION")
            ),
        )
        .map_err(|error| format!("failed to initialize application config: {error}"))?;
    }

    Ok(root)
}

impl Drop for BackendManager {
    fn drop(&mut self) {
        self.terminate_job();
        #[cfg(windows)]
        unsafe {
            if self.job_handle != 0 {
                let _ = CloseHandle(self.job_handle as HANDLE);
            }
        }
    }
}

#[cfg(debug_assertions)]
fn backend_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend")
}

#[cfg(debug_assertions)]
fn python_path(backend_dir: &std::path::Path) -> PathBuf {
    if let Ok(path) = std::env::var("ASH_BACKEND_PYTHON") {
        return PathBuf::from(path);
    }
    backend_dir.join(".venv").join("Scripts").join("python.exe")
}

#[cfg(debug_assertions)]
/// 解析开发模式下从 backend venv 启动 sidecar 的参数。
fn resolve_launch_spec(_app: &AppHandle) -> Result<BackendLaunchSpec, String> {
    let backend_dir = backend_dir();
    let python = python_path(&backend_dir);
    if !python.is_file() {
        return Err(format!(
            "Python sidecar interpreter not found at {}",
            python.display()
        ));
    }

    Ok(BackendLaunchSpec {
        executable: python,
        working_directory: backend_dir.clone(),
        arguments: vec!["-m".to_owned(), "app.main".to_owned()],
        log_directory: backend_dir.join(".runtime"),
    })
}

#[cfg(not(debug_assertions))]
/// 解析发布模式下随 Tauri resource 打包的 sidecar。
fn resolve_launch_spec(app: &AppHandle) -> Result<BackendLaunchSpec, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("failed to resolve Tauri resource directory: {error}"))?;
    let backend_dir = resource_dir.join("backend");
    let executable = backend_dir.join("ai-study-helper-backend.exe");
    if !executable.is_file() {
        return Err(format!(
            "packaged sidecar not found at {}",
            executable.display()
        ));
    }
    let log_directory = app
        .path()
        .app_log_dir()
        .map_err(|error| format!("failed to resolve application log directory: {error}"))?;

    Ok(BackendLaunchSpec {
        executable,
        working_directory: backend_dir,
        arguments: Vec::new(),
        log_directory,
    })
}

#[cfg(all(feature = "eval-console", debug_assertions))]
/// 解析开发模式下的评测 sidecar 启动参数。
fn resolve_eval_launch_spec(_app: &AppHandle) -> Result<BackendLaunchSpec, String> {
    let backend_dir = backend_dir();
    let python = python_path(&backend_dir);
    if !python.is_file() {
        return Err(format!(
            "Python evaluation sidecar interpreter not found at {}",
            python.display()
        ));
    }
    Ok(BackendLaunchSpec {
        executable: python,
        working_directory: backend_dir.clone(),
        arguments: vec!["-m".to_owned(), "eval_main".to_owned()],
        log_directory: backend_dir.join(".runtime"),
    })
}

#[cfg(all(feature = "eval-console", not(debug_assertions)))]
/// 解析显式评测构建中的独立 sidecar。
fn resolve_eval_launch_spec(app: &AppHandle) -> Result<BackendLaunchSpec, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("failed to resolve Tauri resource directory: {error}"))?;
    let backend_dir = resource_dir.join("evaluation");
    let executable = backend_dir.join("ai-study-helper-evaluation.exe");
    if !executable.is_file() {
        return Err(format!(
            "packaged evaluation sidecar not found at {}",
            executable.display()
        ));
    }
    let log_directory = app
        .path()
        .app_log_dir()
        .map_err(|error| format!("failed to resolve application log directory: {error}"))?;
    Ok(BackendLaunchSpec {
        executable,
        working_directory: backend_dir,
        arguments: Vec::new(),
        log_directory,
    })
}

/// 申请一个仅绑定回环地址的临时端口。
fn free_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("failed to reserve loopback port: {error}"))?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("failed to read reserved port: {error}"))
}

#[tauri::command]
/// Tauri 命令：返回前端连接 sidecar 所需的地址和令牌。
fn backend_connection(state: State<'_, Arc<BackendManager>>) -> BackendConnection {
    state.connection()
}

#[tauri::command]
/// Tauri 命令：返回 sidecar 当前状态。
fn backend_status(state: State<'_, Arc<BackendManager>>) -> BackendStatus {
    state.status()
}

#[cfg(feature = "eval-console")]
#[tauri::command]
/// Tauri 命令：首次访问时启动评测 sidecar 并返回独立连接。
fn eval_backend_connection(
    app: AppHandle,
    state: State<'_, Arc<EvaluationBackendManager>>,
) -> BackendConnection {
    state.ensure_started(&app);
    state.connection()
}

#[cfg(feature = "eval-console")]
#[tauri::command]
/// Tauri 命令：返回评测 sidecar 的最近状态。
fn eval_backend_status(state: State<'_, Arc<EvaluationBackendManager>>) -> BackendStatus {
    state.status()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
/// 构建并运行桌面应用，退出时确保 sidecar 进程树被清理。
pub fn run() {
    let port = free_loopback_port().unwrap_or_else(|error| panic!("{error}"));
    let manager = Arc::new(
        BackendManager::new(port, Uuid::new_v4().simple().to_string())
            .unwrap_or_else(|error| panic!("{error}")),
    );
    #[cfg(feature = "eval-console")]
    let eval_manager = Arc::new(
        EvaluationBackendManager::new(
            free_loopback_port().unwrap_or_else(|error| panic!("{error}")),
            Uuid::new_v4().simple().to_string(),
        )
        .unwrap_or_else(|error| panic!("{error}")),
    );

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Arc::clone(&manager));
    #[cfg(feature = "eval-console")]
    let builder =
        builder
            .manage(Arc::clone(&eval_manager))
            .invoke_handler(tauri::generate_handler![
                backend_connection,
                backend_status,
                eval_backend_connection,
                eval_backend_status
            ]);
    #[cfg(not(feature = "eval-console"))]
    let builder =
        builder.invoke_handler(tauri::generate_handler![backend_connection, backend_status]);

    #[cfg(feature = "eval-console")]
    let eval_setup = Arc::clone(&eval_manager);
    let app = builder
        .setup({
            let manager = Arc::clone(&manager);
            move |app| {
                let data_root =
                    prepare_app_data_root(app.handle()).map_err(std::io::Error::other)?;
                manager.start(app.handle().clone(), data_root.clone());
                #[cfg(feature = "eval-console")]
                {
                    let eval_root = data_root.join("eval-data");
                    eval_setup.configure_roots(data_root, eval_root);
                }
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Tauri application");

    #[cfg(feature = "eval-console")]
    let eval_runtime = Arc::clone(&eval_manager);
    app.run(move |app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            manager.shutdown(app_handle);
            #[cfg(feature = "eval-console")]
            eval_runtime.shutdown(app_handle);
        }
    });
}
