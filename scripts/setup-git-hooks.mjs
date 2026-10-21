#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { chmodSync } from "node:fs";
import { join, resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "..");

try {
  execFileSync("git", ["rev-parse", "--git-dir"], {
    cwd: repositoryRoot,
    stdio: "ignore",
  });
} catch {
  console.log("当前目录不是 Git 工作区，跳过 Git hooks 配置。");
  process.exit(0);
}

try {
  if (process.platform !== "win32") {
    chmodSync(join(repositoryRoot, ".githooks", "pre-commit"), 0o755);
  }
  execFileSync("git", ["config", "core.hooksPath", ".githooks"], {
    cwd: repositoryRoot,
    stdio: "inherit",
  });
  console.log("Git hooks 已配置为 .githooks。");
} catch (error) {
  console.error(`Git hooks 配置失败：${String(error)}`);
  process.exitCode = 1;
}
