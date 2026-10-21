#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

const cargoExecutable =
  process.env.CARGO ??
  (process.platform === "win32"
    ? join(homedir(), ".cargo", "bin", "cargo.exe")
    : "cargo");
const cargoDirectory =
  process.platform === "win32" ? join(homedir(), ".cargo", "bin") : undefined;
const environment = {
  ...process.env,
  PATH: cargoDirectory
    ? `${cargoDirectory}${delimiter}${process.env.PATH ?? ""}`
    : process.env.PATH,
};

// 统一质量入口必须显式定位工具链，不能依赖调用者是否预先配置 shell PATH。
const result = spawnSync(cargoExecutable, process.argv.slice(2), {
  env: environment,
  stdio: "inherit",
});

if (result.error) {
  console.error(`无法执行 Cargo：${String(result.error)}`);
  process.exitCode = 1;
} else {
  process.exitCode = result.status ?? 1;
}
