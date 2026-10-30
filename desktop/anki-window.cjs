"use strict";
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const execute = promisify(execFile);

// MainWindowHandle excludes hidden windows. Enumerate the selected program's
// windows instead, so an earlier hidden launch can be recovered without exit.
const focusScript = String.raw`
$ErrorActionPreference = 'Stop'
$ankiProcesses = @(Get-Process -Name anki -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $env:ANKISENSE_TARGET_ANKI })
if ($ankiProcesses.Count -eq 0) { '{"visible":false,"activated":false}'; exit }
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class AnkiSenseWindow {
    private delegate bool EnumCallback(IntPtr window, IntPtr value);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumCallback callback, IntPtr value);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")] private static extern IntPtr GetLastActivePopup(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindowAsync(IntPtr window, int command);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    public static bool[] Show(int[] processes) {
        var ids = new HashSet<int>(processes);
        var windows = new List<IntPtr>();
        EnumWindows(delegate(IntPtr window, IntPtr value) {
            uint process;
            GetWindowThreadProcessId(window, out process);
            if (!ids.Contains((int)process) || GetWindow(window, 4) != IntPtr.Zero) return true;
            var title = new StringBuilder(512);
            if (GetWindowText(window, title, title.Capacity) > 0) {
                if (title.ToString().IndexOf("Anki", StringComparison.OrdinalIgnoreCase) >= 0) windows.Insert(0, window);
                else windows.Add(window);
            }
            return true;
        }, IntPtr.Zero);
        if (windows.Count == 0) return new bool[] { false, false };
        var main = windows[0];
        ShowWindowAsync(main, IsIconic(main) ? 9 : 5);
        for (int attempt = 0; attempt < 10 && !IsWindowVisible(main); attempt++) Thread.Sleep(50);
        var popup = GetLastActivePopup(main);
        var target = popup != IntPtr.Zero && IsWindowVisible(popup) ? popup : main;
        return new bool[] { IsWindowVisible(main), SetForegroundWindow(target) };
    }
}
'@
$result = [AnkiSenseWindow]::Show([int[]]@($ankiProcesses.Id))
@{ visible = $result[0]; activated = $result[1] } | ConvertTo-Json -Compress
`;

async function showAnkiWindow(executable, { run = execute, platform = process.platform } = {}) {
  if (platform !== "win32") return { visible: false, activated: false };
  const { stdout } = await run("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", focusScript], {
    windowsHide: true, timeout: 8000, env: { ...process.env, ANKISENSE_TARGET_ANKI: executable },
  });
  const result = JSON.parse(stdout.trim());
  return { visible: result.visible === true, activated: result.activated === true };
}
module.exports = { showAnkiWindow, focusScript };
