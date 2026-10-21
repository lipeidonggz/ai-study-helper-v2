#!/usr/bin/env node

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "..");
const sourceRoots = [
  join(repositoryRoot, "frontend", "src"),
  join(repositoryRoot, "desktop", "src-tauri", "src"),
  join(repositoryRoot, "backend", "app"),
];
const sourceExtensions = new Set([".ts", ".vue", ".rs", ".py"]);
const failures = [];

/** 递归列出需要检查的源码文件。 */
function collectSourceFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) {
      return collectSourceFiles(path);
    }
    const extension = path.slice(path.lastIndexOf("."));
    return sourceExtensions.has(extension) ? [path] : [];
  });
}

/** 从 Vue 单文件组件中提取 TypeScript script。 */
function typescriptSource(path, source) {
  if (!path.endsWith(".vue")) {
    return source;
  }
  const match = source.match(/<script\s+setup\s+lang=["']ts["']>([\s\S]*?)<\/script>/);
  return match?.[1] ?? "";
}

/** 检查顶层 TypeScript 边界类型和函数是否紧邻 JSDoc。 */
function checkTypeScriptDocumentation(path, source) {
  const lines = source.split(/\r?\n/u);
  const declaration = /^(?:export\s+)?(?:interface|type|(?:async\s+)?function)\s+/u;
  lines.forEach((line, index) => {
    if (!declaration.test(line)) {
      return;
    }
    const previous = lines
      .slice(0, index)
      .reverse()
      .find((candidate) => candidate.trim() !== "");
    if (!previous || !previous.trim().startsWith("/**")) {
      failures.push(
        `${relative(repositoryRoot, path)}:${index + 1}: 公共 TypeScript 声明缺少紧邻的 JSDoc`,
      );
    }
  });
}

/** 检查 TODO 是否同时写明原因和触发条件。 */
function checkTodoContext(path, source) {
  source.split(/\r?\n/u).forEach((line, index) => {
    if (!line.includes("TODO")) {
      return;
    }
    if (!/原因[:：]/u.test(line) || !/触发条件[:：]/u.test(line)) {
      failures.push(
        `${relative(repositoryRoot, path)}:${index + 1}: TODO 必须包含“原因：”和“触发条件：”`,
      );
    }
  });
}

/** 拦截已知会导致文档与实现分叉的过期术语。 */
function checkStalePhrases(path, source) {
  const forbidden = [/preview-only/iu, /preview jobs/iu];
  source.split(/\r?\n/u).forEach((line, index) => {
    if (forbidden.some((pattern) => pattern.test(line))) {
      failures.push(
        `${relative(repositoryRoot, path)}:${index + 1}: 发现过期的 preview-only 描述`,
      );
    }
  });
}

for (const root of sourceRoots) {
  for (const path of collectSourceFiles(root)) {
    const source = readFileSync(path, "utf8");
    checkTodoContext(path, source);
    checkStalePhrases(path, source);
    if (path.endsWith(".ts") || path.endsWith(".vue")) {
      checkTypeScriptDocumentation(path, typescriptSource(path, source));
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log("注释检查通过。");
}
