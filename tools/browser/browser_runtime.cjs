"use strict";

const fs = require("node:fs");
const path = require("node:path");

const project = path.resolve(__dirname, "../..");

// Resolve explicit relative paths against the project, not the caller's cwd.
function configuredExecutable(value) {
  const executable = value.trim();
  return executable.includes("/") || executable.includes("\\")
    ? path.resolve(project, executable)
    : executable;
}

function pythonExecutable() {
  if (process.env.ANKISENSE_PYTHON?.trim()) {
    return configuredExecutable(process.env.ANKISENSE_PYTHON);
  }
  const executable = process.platform === "win32"
    ? path.join(project, ".venv", "Scripts", "python.exe")
    : path.join(project, ".venv", "bin", "python");
  if (!fs.existsSync(executable)) {
    throw new Error("Project Python environment is missing. Create .venv and install requirements.lock.txt, or set ANKISENSE_PYTHON to your Python executable.");
  }
  return executable;
}

function browserOptions() {
  const options = { headless: true, args: ["--disable-gpu", "--disable-extensions"] };
  // Without an override, use Chromium installed by `npx playwright install chromium`.
  if (process.env.CHROME_PATH?.trim()) {
    options.executablePath = path.resolve(project, process.env.CHROME_PATH.trim());
  }
  return options;
}

module.exports = { project, pythonExecutable, browserOptions };
