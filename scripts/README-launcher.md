# Linux desktop launcher

The launcher uses the logged-in user's systemd manager on Linux (including
Pop!_OS/COSMIC on Wayland). It uses existing dependencies and never installs them.
It starts no terminal windows and needs no interactive shell at launch time.

## Install

From the development repository, with your existing Node installation on PATH:

```bash
./scripts/relayops install
```

This writes three units to `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`
and a **RelayOps** menu entry to
`${XDG_DATA_HOME:-$HOME/.local/share}/applications/relayops.desktop`, using the
existing RelayOps SVG icon. It captures absolute repository and Node paths.
No services are started, enabled at boot, or restarted by installation. Existing
units with different content are rejected; review them before replacing them.
Keep the repository at its installed path; reinstall after moving it or changing
Node versions. No sudo or desktop shell profile is needed.

Requirements: existing `backend/.venv`, backend configuration with a real
`SECRET_KEY`, frontend `node_modules`, Node, systemd user session, and `xdg-open`.
The installer uses a Node executable on PATH, or the newest installed nvm version
if PATH does not contain Node. It does not run nvm or install Node.

The frontend uses an existing `.next/BUILD_ID` production build when installed;
otherwise it uses development mode. Reinstall while launcher services are stopped
to change modes. Do not build over files used by an active frontend.

## Use

Click **RelayOps** in the application menu, or run from the repository:

```bash
./scripts/relayops start
./scripts/relayops status
./scripts/relayops logs
./scripts/relayops stop
```

`start` defaults when no command is given. It starts backend port **8001** and
frontend port **3002**, waits up to 90 seconds for both HTTP endpoints, then starts
one worker and opens <http://127.0.0.1:3002>. The frontend proxy targets 8001.
Units supply exact dashboard origins for localhost/loopback port 3002 without
editing `.env`. The backend reads its existing `.env`; no database is reset or
existing permissions changed by the launcher. Normal application startup still
applies its existing database-creation policy.

**Starting the worker processes queued real deliveries.** Installation and status
checks do not. Do not start for testing against a live database unless deliveries
are intended. The launcher does not touch n8n (5678) or services on 3000/8000.

A runtime-directory lock serializes launcher operations. Stable unit names prevent
repeat service launches; the worker's existing database lock prevents another
sending worker for that same database. Unmanaged listeners on 8001/3002 cause a
clear failure. Existing manually launched services are never adopted or killed.
Startup failure stops only units newly started by that invocation. `stop` addresses
only the three `relayops-*.service` units, with up to 90 seconds for graceful shutdown.
No automatic restart is configured, so unexpected exits require inspection and
another explicit start. Services survive launcher/browser exit and normally end
with the user's login session; no linger or boot startup is configured.

Logs go to the user journal; launcher locking uses `$XDG_RUNTIME_DIR`. Application
data stays at its existing configured location. No new log/state files are stored
in the repository. `logs` prints the latest 100 journal entries; follow continuously:

```bash
journalctl --user -f -u relayops-backend -u relayops-frontend -u relayops-worker
```

## Dashboard window

After service readiness, the launcher prefers the existing Google Chrome Flatpak:

```bash
flatpak run com.google.Chrome --app=http://127.0.0.1:3002
```

This opens a standalone Chrome app window without the normal browser toolbar.
If Flatpak/Chrome is unavailable or the launch immediately fails, it falls back to
`xdg-open` and the default browser. Nothing is installed. Browser launch failures
do not stop healthy services. Failures occurring after the initial one-second
launch check must be investigated in the desktop session.

Repeated clicks within five seconds of a successful launch are suppressed using
`$XDG_RUNTIME_DIR/relayops-browser-opened`. This is a best-effort debounce, not
window tracking: later clicks can open another window, and closing a window within
five seconds requires waiting before reopening. Wayland does not provide a reliable
generic window-focus mechanism. The existing launcher lock still serializes starts.
No launcher reinstall is needed for this script-only browser change when the menu
entry already points to this checkout.

## Troubleshooting and removal

- If 8001/3002 is already occupied, inspect the owner yourself. Existing RelayOps
  processes require a separately approved handover; do not stop unrelated services.
- If the menu entry is not immediately visible, refresh the application menu or
  log in again. Launch errors use desktop notifications when `notify-send` exists;
  otherwise run the command in a terminal for the error text.
- `Failed to connect to bus`: run in the logged-in desktop user's session, without
  sudo. Check `systemctl --user is-system-running` and `$XDG_RUNTIME_DIR`.
- Worker exits: check logs for an existing database lock, settings, or permissions.
  Do not delete lock files or change database permissions to force startup.
- Readiness failure: inspect `status` and `logs`. Confirm the existing build and
  dependencies work; the launcher does not install or repair them.
- To uninstall, first explicitly stop launcher-managed services. Remove only the
  three installed `relayops-{backend,frontend,worker}.service` files and
  `relayops.desktop` from the directories above, then run
  `systemctl --user daemon-reload`. Leave application data and dependencies intact.

Isolated tests (no services or databases touched):

```bash
python3 -m unittest discover -s scripts -p 'test_relayops_launcher.py' -v
```
