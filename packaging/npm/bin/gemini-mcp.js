#!/usr/bin/env node
"use strict";
/* @vernikr/gemini-mcp — thin bootstrap: runs the pinned Python core
   (PyPI dist: gemini-mcp-gateway) through uv. No core logic lives here.
   Version lockstep: this package's version == the PyPI pin. */
const { spawnSync } = require("node:child_process");
const CORE = "gemini-mcp-gateway";
const VER = require("../package.json").version;

function uvx() {
  const hit = spawnSync("sh", ["-c", "command -v uvx"], { encoding: "utf8" });
  if (hit.status === 0 && hit.stdout.trim()) return [hit.stdout.trim(), []];
  return ["npx", ["-y", "@astral-sh/uv", "uvx"]];   // uv self-installs via npm
}
const [cmd, base] = uvx();
const res = spawnSync(cmd, [...base, `${CORE}==${VER}`, ...process.argv.slice(2)],
  { stdio: "inherit" });
if (res.error) {
  console.error(`gemini-mcp: bootstrap failed: ${res.error.message}`);
  console.error("hint: install uv once: curl -LsSf https://astral.sh/uv/install.sh | sh");
  process.exit(1);
}
process.exit(res.status == null ? 1 : res.status);
