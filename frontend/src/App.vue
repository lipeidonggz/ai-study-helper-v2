<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

type ManagerStatus =
  | "starting"
  | "running"
  | "restarting"
  | "failed"
  | "stopped";

interface BackendStatus {
  status: ManagerStatus;
  port: number;
  restartCount: number;
  message: string | null;
}

interface BackendConnection {
  baseUrl: string;
  token: string;
}

interface RuntimeInfo {
  status: string;
  version: string;
  pid: number;
  uptime_seconds: number;
}

const managerStatus = ref<ManagerStatus>("starting");
const connection = ref<BackendConnection | null>(null);
const runtime = ref<RuntimeInfo | null>(null);
const restartCount = ref(0);
const message = ref("正在连接本地后端…");

let unlisten: UnlistenFn | undefined;
let healthTimer: number | undefined;
let disposed = false;

const stateLabel = computed(() => {
  switch (managerStatus.value) {
    case "starting":
      return "正在启动";
    case "running":
      return runtime.value ? "运行正常" : "等待健康检查";
    case "restarting":
      return "正在恢复";
    case "failed":
      return "启动失败";
    case "stopped":
      return "已停止";
  }
});

const stateClass = computed(() => {
  const visualStatus =
    managerStatus.value === "running" && !runtime.value
      ? "starting"
      : managerStatus.value;
  return `status-${visualStatus}`;
});

async function checkRuntime(): Promise<void> {
  if (!connection.value || disposed) {
    return;
  }

  try {
    const response = await fetch(`${connection.value.baseUrl}/api/runtime`, {
      headers: {
        "x-launch-token": connection.value.token,
      },
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    runtime.value = (await response.json()) as RuntimeInfo;
    if (managerStatus.value === "running") {
      message.value = "Tauri 已连接本地 Python sidecar。";
    }
  } catch (error) {
    runtime.value = null;
    if (managerStatus.value === "running") {
      message.value = `健康检查失败：${String(error)}`;
    }
  }
}

async function connect(): Promise<void> {
  try {
    connection.value = await invoke<BackendConnection>("backend_connection");
    const status = await invoke<BackendStatus>("backend_status");
    applyStatus(status);
    await checkRuntime();
  } catch (error) {
    managerStatus.value = "failed";
    message.value = `无法读取 Tauri 后端状态：${String(error)}`;
  }
}

function applyStatus(status: BackendStatus): void {
  managerStatus.value = status.status;
  restartCount.value = status.restartCount;
  message.value =
    status.message ??
    (status.status === "restarting"
      ? `sidecar 意外退出，正在第 ${status.restartCount} 次重启。`
      : status.status === "running"
        ? "本地后端已启动，正在进行健康检查。"
        : "正在连接本地后端…");
}

onMounted(async () => {
  unlisten = await listen<BackendStatus>("backend-status", (event) => {
    applyStatus(event.payload);
    if (event.payload.status === "running") {
      void checkRuntime();
    }
  });

  await connect();
  healthTimer = window.setInterval(() => {
    void checkRuntime();
  }, 1500);
});

onBeforeUnmount(() => {
  disposed = true;
  unlisten?.();
  if (healthTimer !== undefined) {
    window.clearInterval(healthTimer);
  }
});
</script>

<template>
  <main class="shell">
    <section class="hero">
      <p class="eyebrow">AI STUDY HELPER V2</p>
      <h1>本地文档学习问答 Agent</h1>
      <p class="intro">
        当前是第一个运行切片：验证桌面壳、本地后端、鉴权与进程生命周期。
        文档入库、检索和问答将在后续切片接入。
      </p>
    </section>

    <section class="status-card">
      <div class="status-heading">
        <span class="status-dot" :class="stateClass" aria-hidden="true"></span>
        <div>
          <p class="status-label">Python sidecar</p>
          <h2>{{ stateLabel }}</h2>
        </div>
      </div>

      <p class="message">{{ message }}</p>

      <dl class="facts">
        <div>
          <dt>监听地址</dt>
          <dd>{{ connection ? `${connection.baseUrl}` : "等待 Tauri 分配" }}</dd>
        </div>
        <div>
          <dt>进程 PID</dt>
          <dd>{{ runtime?.pid ?? "—" }}</dd>
        </div>
        <div>
          <dt>后端版本</dt>
          <dd>{{ runtime?.version ?? "—" }}</dd>
        </div>
        <div>
          <dt>自动重启</dt>
          <dd>{{ restartCount }} 次</dd>
        </div>
      </dl>
    </section>

    <section class="next-step">
      <p class="next-label">NEXT SLICE</p>
      <h2>Windows 打包与 sidecar 发布链</h2>
      <p>
        当前切片验证开发模式。下一步将把 Python 后端打成 PyInstaller
        one-folder sidecar，验证正式应用能够随安装包启动、退出并清理进程。
      </p>
    </section>
  </main>
</template>
