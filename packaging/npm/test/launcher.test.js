"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { autoUpdateDisabled, isNewer, versionParts } = require("../bin/gemini.js");

test("launcher compares published core versions without downgrading", () => {
  assert.deepEqual(versionParts("1.2.3"), [1, 2, 3]);
  assert.equal(isNewer("0.28.0", "0.27.9"), true);
  assert.equal(isNewer("0.27.0", "0.27.0"), false);
  assert.equal(isNewer("0.27.0", "0.28.0"), false);
  assert.equal(isNewer("not-a-version", "0.1.0"), false);
});

test("--no-update and GEMINI_AUTO_UPDATE opt out", () => {
  const before = process.env.GEMINI_AUTO_UPDATE;
  try {
    delete process.env.GEMINI_AUTO_UPDATE;
    assert.equal(autoUpdateDisabled([]), false);
    assert.equal(autoUpdateDisabled(["stdio", "--no-update"]), true);
    process.env.GEMINI_AUTO_UPDATE = "off";
    assert.equal(autoUpdateDisabled(["stdio"]), true);
  } finally {
    if (before === undefined) delete process.env.GEMINI_AUTO_UPDATE;
    else process.env.GEMINI_AUTO_UPDATE = before;
  }
});
