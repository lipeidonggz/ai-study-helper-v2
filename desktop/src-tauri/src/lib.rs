use serde::Serialize;
use std::{
    fs::OpenOptions,
    mem::size_of,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, State};
use uuid::Uuid;

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
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

struct BackendManager {
    port: u16,
    token: String,
    job_handle: usize,
    child: Mutex<Option<Child>>,
    shutting_down: AtomicBool,
    restart_count: AtomicU32,
    started_at: Mutex<Option<Instant>>,
    last_status: Mutex<BackendStatus>,
}

impl BackendManager {
    fn new(port: u16, token: String) -> Result<Self, String> {
        Ok(Self {
            port,
            token,
            job_handle: create_job_object()?,
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

    fn connection(&self) -> BackendConnection {
        BackendConnection {
            base_url: format!("http://127.0.0.1:{}", self.port),
            token: self.token.clone(),
        }
    }

    fn status(&self) -> BackendStatus {
        self.last_status
            .lock()
            .expect("backend status lock poisoned")
            .clone()
    }

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

    fn spawn_child(&self) -> Result<Child, String> {
        let backend_dir = backend_dir();
        let python = python_path(&backend_dir);
        if !python.is_file() {
            return Err(format!(
                "Python sidecar interpreter not found at {}",
                python.display()
            ));
        }

        let runtime_dir = backend_dir.join(".runtime");
        std::fs::create_dir_all(&runtime_dir)
            .map_err(|error| format!("failed to create runtime directory: {error}"))?;
        let log_path = runtime_dir.join("sidecar.log");
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| format!("failed to open sidecar log: {error}"))?;
        let error_log = log
            .try_clone()
            .map_err(|error| format!("failed to clone sidecar log: {error}"))?;

        let mut command = Command::new(&python);
        command
            .current_dir(&backend_dir)
            .arg("-m")
            .arg("app.main")
            .arg("--port")
            .arg(self.port.to_string())
            .env("ASH_LAUNCH_TOKEN", &self.token)
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

    fn start(self: &Arc<Self>, app: AppHandle) {
        self.set_status(&app, "starting", None);
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

    fn terminate_job(&self) {
        #[cfg(windows)]
        unsafe {
            let _ = TerminateJobObject(self.job_handle as HANDLE, 1);
        }
    }
}

#[cfg(windows)]
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
fn create_job_object() -> Result<usize, String> {
    Ok(0)
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

fn backend_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend")
}

fn python_path(backend_dir: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("ASH_BACKEND_PYTHON") {
        return PathBuf::from(path);
    }
    backend_dir.join(".venv").join("Scripts").join("python.exe")
}

fn free_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("failed to reserve loopback port: {error}"))?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("failed to read reserved port: {error}"))
}

#[tauri::command]
fn backend_connection(state: State<'_, Arc<BackendManager>>) -> BackendConnection {
    state.connection()
}

#[tauri::command]
fn backend_status(state: State<'_, Arc<BackendManager>>) -> BackendStatus {
    state.status()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let port = free_loopback_port().unwrap_or_else(|error| panic!("{error}"));
    let manager = Arc::new(
        BackendManager::new(port, Uuid::new_v4().simple().to_string())
            .unwrap_or_else(|error| panic!("{error}")),
    );

    let app = tauri::Builder::default()
        .manage(Arc::clone(&manager))
        .invoke_handler(tauri::generate_handler![backend_connection, backend_status])
        .setup({
            let manager = Arc::clone(&manager);
            move |app| {
                manager.start(app.handle().clone());
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Tauri application");

    app.run(move |app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            manager.shutdown(app_handle);
        }
    });
}
