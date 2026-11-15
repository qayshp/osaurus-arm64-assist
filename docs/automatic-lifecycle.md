# Automatic lifecycle with Osaurus

Companion 0.1.2 enables automatic startup and shutdown with Osaurus by default. After installation and provider setup, open Osaurus normally and select a companion profile. The watcher starts the companion server, waits for readiness, and refreshes Osaurus's provider discovery. Model weights and CPU/GPU/NPU contexts load when an inference request selects a profile.

Your existing Osaurus shortcuts and the app's own launch-at-login preference remain in place. This feature adds its own per-user Windows Startup shortcut for a hidden `pythonw` watcher. It does not require administrator access.

## App lifetime, tray behavior and model lifetime

| Situation | Companion behavior |
|---|---|
| Sign in to Windows while Osaurus is closed | The small watcher waits for the app |
| Open the official Store Osaurus app in the same user session | The watcher starts the companion server and refreshes provider discovery when ready |
| Minimize Osaurus or close its window to the system tray | The app and companion remain available |
| Stop using a loaded model | The companion applies the [idle interval selected in Osaurus](idle-policy.md) |
| Actually quit Osaurus | After a short confirmation delay, the watcher stops the companion server and model workers |
| Open Osaurus again in the same sign-in session | The waiting watcher starts the companion again |

The watcher follows the verified `Osaurus.App.exe` from the official Store package in the same Windows user session. The presence of a similarly named process elsewhere or the visibility of a window is insufficient. This lets tray operation continue while distinguishing it from a true app exit.

The watcher remains running while waiting for the next launch. It has a memory cost, and a ready server has its own overhead. Neither state alone allocates the large model weights or NPU contexts used for inference. Resource and startup timings must be measured on the actual machine; the earlier model benchmarks do not measure this new watcher.

Keep Osaurus running while using the companion through another client. A true Osaurus exit also ends the companion's active service lifetime. Manual companion stop and uninstall likewise stop its model workers.

## What setup adds

The normal installer enables one owned shortcut in the current user's Windows Startup folder. It is associated with the companion installation's GUID, so disabling or uninstalling this feature can target that shortcut while preserving unrelated startup items. Windows starts the hidden watcher at sign-in. Setup also starts the watcher for the current session.

The shortcut belongs to the companion. It does not replace an Osaurus shortcut or enable Osaurus itself at sign-in. If you already enable Osaurus's own startup preference, the watcher follows that app launch; otherwise it waits until you open Osaurus yourself.

Updates respect an entry disabled in **Windows Settings → Apps → Startup** or Task Manager's **Startup apps** tab; the installer does not force it to resume. Re-enable it there when that Windows-level choice should change.

The watcher starts the main companion server only for the verified app lifetime. The server retains its existing on-demand model loading and one-worker execution policy. Managed startup refreshes Osaurus's model discovery after the listener is ready, so normal use does not require a separate model-list refresh step.

## Disable automatic management

For the default installation, run this from the source checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Disable-AutoStart.ps1
```

For a custom installation, add `-RootPath <installation-directory>`. The helper removes only the owned startup entry, records the disabled preference, and stops the watcher and the server it manages. An untracked manually started server is preserved. This is the persistent opt-out. A disabled `auto_start: false` value is retained by companion configuration backup and restore.

For an advanced manual installation, opt out during setup:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install.ps1 -NoAutoStart
```

The manual `Start.ps1` and `Stop.ps1` commands remain available. `Start.ps1` starts only a manual server. `Stop.ps1` stops the watcher first, followed by the owned server and model workers, while leaving the login shortcut available for future sign-ins. Use `Disable-AutoStart.ps1` when the automatic login behavior should also remain disabled.

To enable management again for the current sign-in session, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Enable-AutoStart.ps1
```

The first enable can restart an existing owned companion server to transfer its lifetime to the watcher. Repeating enable with a healthy watcher is idempotent. The owned login entry supports later sign-ins according to your Windows Startup Apps choice. Normal daily use needs neither helper command: open or quit Osaurus normally.

## Status and troubleshooting

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Status.ps1 -Json
```

Inspect the reported startup/watcher state, server readiness and idle policy. The companion's local logs provide startup and shutdown details. A running watcher waiting for Osaurus differs from a running server, and either differs from a loaded model.

If the server does not follow the app, check that the official Store app is running in the same Windows user session and that automatic management is enabled. A deliberate manual stop ends the current watcher; a future sign-in can start it again through the retained login shortcut. Diagnose stale identities or blocked startup from the helper's state/logs instead of stopping every Python process.

## Why a Windows watcher

The checked [Windows CLI/import interface](https://docs.osaurus.ai/windows/cli#osaurus-import) does not provide a documented executable lifecycle hook for an arbitrary OpenAI-compatible provider. Arbitrary bundle runtimes/executables are refused, and `serve --supervise` is Mac-only. MCP stdio starts MCP tool servers, which is a different protocol contract.

Microsoft documents the [current-user Startup folder](https://support.microsoft.com/en-us/windows/experience/startup-boot/configure-startup-applications-in-windows) as a supported way to start an application at sign-in. The companion uses that separate owned entry to preserve the user's existing app launch choices.

Osaurus documents that [Windows automation continues while the app is in the system tray](https://docs.osaurus.ai/windows/automation#when-they-run), and that [`osaurus stop` quits the Windows app](https://docs.osaurus.ai/windows/cli#osaurus-stop). The watcher therefore follows actual process lifetime. The prior 0.1.0 acceptance receipts and 0.1.1 idle-policy evidence retain their original scope; they do not certify the 0.1.2 lifecycle behavior.
