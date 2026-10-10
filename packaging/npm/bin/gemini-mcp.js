#!/usr/bin/env node
"use strict";
/* @vernikr/gemini-mcp — thin bootstrap: runs the pinned Python core
   (PyPI dist: gemini-mcp-gateway) through uv. No core logic lives here.
   Version lockstep: this package's version == the PyPI pin. */
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const CORE = "gemini-mcp-gateway";
const VER = require("../package.json").version;
const win = process.platform === "win32";
const HINT = "hint: install uv once: " + (win
  ? 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
  : "curl -LsSf https://astral.sh/uv/install.sh | sh");

function findUvx() {
  if (!win) {
    const hit = spawnSync("sh", ["-c", "command -v uvx"], { encoding: "utf8" });
    if (hit.status === 0 && hit.stdout.trim()) return hit.stdout.trim();
  }
  // MCP clients often launch with a minimal PATH — probe the usual homes too.
  for (const d of [path.join(os.homedir(), ".local", "bin"),
                   path.join(os.homedir(), ".cargo", "bin"),
                   "/opt/homebrew/bin", "/usr/local/bin"]) {
    const p = path.join(d, win ? "uvx.exe" : "uvx");
    if (fs.existsSync(p)) return p;
  }
  return null;
}

let uvx = findUvx();
if (!uvx) {
  // uv is NOT distributed via npm — bootstrap with the official standalone
  // installer (one time), without touching shell rc files.
  console.error("gemini-mcp: uv not found — running the official astral.sh installer (one time)…");
  const r = win
    ? spawnSync("powershell", ["-ExecutionPolicy", "ByPass", "-c",
        "irm https://astral.sh/uv/install.ps1 | iex"], { stdio: "inherit" })
    : spawnSync("sh", ["-c",
        "curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh"],
        { stdio: "inherit" });
  uvx = (r.status === 0 && findUvx()) || null;
}
if (!uvx) {
  console.error(`gemini-mcp: bootstrap failed: uv unavailable\n${HINT}`);
  process.exit(1);
}
// bin name != dist name: uvx needs --from (verified against PyPI 0.7.1)
const res = spawnSync(uvx, ["--from", `${CORE}==${VER}`, "gemini-mcp",
  ...process.argv.slice(2)], { stdio: "inherit" });
if (res.error) {
  console.error(`gemini-mcp: bootstrap failed: ${res.error.message}\n${HINT}`);
  process.exit(1);
}
process.exit(res.status == null ? 1 : res.status);
