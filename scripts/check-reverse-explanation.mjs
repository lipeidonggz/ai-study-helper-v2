#!/usr/bin/env node

import { execFileSync } from "node:child_process";

const codePrefixes = ["backend/app/", "frontend/src/", "desktop/src-tauri/src/"];
const slicePrefix = "docs/development/slices/";
const gatePattern = /<!-- reverse-explanation-gate: passed (\d+)\/(\d+) -->/u;

/** 读取 stage 中的文件路径。 */
function stagedPaths() {
  const output = execFileSync("git", ["diff", "--cached", "--name-only", "-z"], {
    encoding: "utf8",
  });
  return output.split("\0").filter(Boolean);
}

/** 读取 stage 版本的文件内容，避免使用未暂存改动。 */
function stagedContent(path) {
  return execFileSync("git", ["show", `:${path}`], { encoding: "utf8" });
}

const paths = stagedPaths();
const changesApplicationCode = paths.some((path) =>
  codePrefixes.some((prefix) => path.startsWith(prefix)),
);
if (!changesApplicationCode) {
  process.exit(0);
}

const slicePaths = paths.filter(
  (path) =>
    path.startsWith(slicePrefix) &&
    path.endsWith(".md") &&
    !path.endsWith("/TEMPLATE.md"),
);
const passedSlices = [];
for (const path of slicePaths) {
  const content = stagedContent(path);
  if (!content.includes("- 状态：已完成")) {
    continue;
  }
  const match = content.match(gatePattern);
  if (match === null) {
    continue;
  }
  const passed = Number(match[1]);
  const total = Number(match[2]);
  if (total >= 3 && passed === total) {
    passedSlices.push(path);
  }
}

if (passedSlices.length === 0) {
  console.error(
    "反向解释门禁未通过：提交业务代码时必须同时提交一个“已完成”切片，并记录至少 3 道已全部通过的反向解释。",
  );
  console.error("记录格式：<!-- reverse-explanation-gate: passed N/N -->");
  process.exit(1);
}

console.log(`反向解释门禁通过：${passedSlices.join(", ")}`);
