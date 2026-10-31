#!/usr/bin/env node
"use strict";
/* @vernikr/gemini launcher: runs the Python CLI through uv and checks PyPI for a
   newer core on each start. The package version remains the offline/opt-out pin. */
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");
const CORE = "vernikr-gemini";
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

function versionParts(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(value || "");
  return match ? match.slice(1, 4).map(Number) : null;
}

function isNewer(candidate, current) {
  const a = versionParts(candidate);
  const b = versionParts(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

function latestCoreVersion(timeoutMs = 1250) {
  return new Promise((resolve) => {
    const req = https.get({
      hostname: "pypi.org",
      path: `/pypi/${CORE}/json`,
      headers: { Accept: "application/json", "User-Agent": "vernikr-gemini-launcher" },
    }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        resolve(null);
        return;
      }
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        body += chunk;
        if (body.length > 256_000) req.destroy(new Error("registry response too large"));
      });
      res.on("end", () => {
        try {
          const version = JSON.parse(body).info.version;
          resolve(typeof version === "string" ? version : null);
        } catch (_) {
          resolve(null);
        }
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error("registry check timed out")));
    req.on("error", () => resolve(null));
  });
}

function autoUpdateDisabled(argv) {
  const value = (process.env.GEMINI_AUTO_UPDATE || "").trim().toLowerCase();
  return argv.includes("--no-update") || ["0", "false", "no", "off"].includes(value);
}

async function main(argv = process.argv.slice(2)) {
  const args = argv.filter((arg) => arg !== "--no-update");
  const introspection = args.some((arg) => ["--version", "--help", "-h"].includes(arg));
  const disabled = autoUpdateDisabled(argv) || introspection;

  let uvx = findUvx();
  if (!uvx) {
    // uv is not distributed via npm — bootstrap with the official standalone installer.
    console.error("gemini: uv not found — running the official astral.sh installer (one time)…");
    const r = win
      ? spawnSync("powershell", ["-ExecutionPolicy", "ByPass", "-c",
          "irm https://astral.sh/uv/install.ps1 | iex"], { stdio: "inherit" })
      : spawnSync("sh", ["-c",
          "curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh"],
          { stdio: "inherit" });
    uvx = (r.status === 0 && findUvx()) || null;
  }
  if (!uvx) {
    console.error(`gemini: bootstrap failed: uv unavailable\n${HINT}`);
    process.exitCode = 1;
    return;
  }

  let coreVersion = VER;
  if (!disabled) {
    const latest = await latestCoreVersion();
    if (latest && isNewer(latest, VER)) {
      const probe = spawnSync(uvx, ["--from", `${CORE}==${latest}`, "gemini", "--version"], {
        stdio: "ignore", timeout: 180_000,
      });
      if (probe.status === 0) {
        coreVersion = latest;
        console.error(`gemini: using updated Python core ${latest}; disable with --no-update ` +
          "or GEMINI_AUTO_UPDATE=0");
      } else {
        console.error(`gemini: update check found ${latest}, but it could not be installed; ` +
          `using ${VER}`);
      }
    }
  }

  const res = spawnSync(uvx, ["--from", `${CORE}==${coreVersion}`, "gemini", ...args], {
    stdio: "inherit",
  });
  if (res.error) {
    console.error(`gemini: bootstrap failed: ${res.error.message}\n${HINT}`);
    process.exitCode = 1;
    return;
  }
  process.exitCode = res.status == null ? 1 : res.status;
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`gemini: launcher failed: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { autoUpdateDisabled, isNewer, latestCoreVersion, versionParts };
