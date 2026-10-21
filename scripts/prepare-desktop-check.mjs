#!/usr/bin/env node

import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "..");

// Tauri 的 build script 在 cargo check 时也会校验资源目录；干净 checkout
// 没有打包 sidecar，因此创建空目录即可满足静态检查，不提交构建产物。
mkdirSync(resolve(repositoryRoot, "desktop", "src-tauri", "resources", "backend"), {
  recursive: true,
});
