"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { project } = require("./browser_runtime.cjs");

// Enumerate explicitly so Windows shells and Node 20 do not need glob expansion.
const tests = fs.readdirSync(path.join(project, "tests"))
  .filter(name => name.endsWith(".test.cjs"))
  .sort()
  .map(name => path.join(project, "tests", name));
if (tests.length === 0) throw new Error("No Node tests found in the project's tests directory.");
const result = spawnSync(process.execPath, ["--test", ...tests], {
  cwd: project, stdio: "inherit", windowsHide: true,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
