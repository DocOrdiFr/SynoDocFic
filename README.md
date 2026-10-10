# SynoDocFic 1.3 (DSM 7 SPK)

This project is a full Synology DSM 7 package source for a batch file renaming web app with folder synchronization.

## Features

### Batch Renamer
- Select target directory on NAS (`/volumeX/...`)
- Optional recursive mode (include subdirectories)
- 3 rename modes via tabs:
  - Remove text from filename
  - Add prefix
  - Add suffix
- Scrollable preview list before execution (`original -> renamed`)
- Confirmation dialog before rename apply
- Safe file handling:
  - No overwrite collisions
  - Duplicate name auto-resolution (`name (1).ext`, `name (2).ext`, ...)
  - Two-phase rename to avoid in-batch conflicts
- Undo last batch (or explicit batch id via API)
- Operation logging and history persistence

### SynchroSyno (Folder Sync)
- Create sync profiles with source & destination paths
- Support for local-to-local synchronization
- Remote paths via SMB/FTP with direct comparison and copy
- Intelligent sync:
  - Compares file size & modification time
  - Skips identical files (no re-copy)
  - Creates directories as needed
- Run profiles individually
- Logs all sync operations

## Project Layout

```text
SynoDocFic/
├── INFO
├── conf/
│   ├── privilege
│   └── resource
├── scripts/
│   ├── preinst
│   ├── postinst
│   ├── preuninst
│   ├── postuninst
│   ├── start-stop-status
│   └── start-stop-status.conf
└── package/
    └── app/
        ├── server.py
        ├── static/
        │   ├── index.html
        │   ├── styles.css
        │   └── app.js
        ├── data/
        └── logs/
```

## Backend API

### `POST /api/preview`
Request:

```json
{
  "directory": "/volume1/photos",
  "recursive": true,
  "mode": "remove",
  "value": "draft",
  "limit": 20000
}
```

Response includes:
- `preview_id`
- `summary` (`rename_actions`, `unchanged`, `auto_renamed`, `truncated`)
- `items[]` with `from`, `to`, `status`

### `POST /api/rename`
Request:

```json
{
  "preview_id": "<id>",
  "confirm": true
}
```

Applies two-phase safe rename and stores a history batch.

### `POST /api/undo`
Request:

```json
{}
```

Undoes the latest non-undone batch (or specify `batch_id`).

### `GET /api/dirs`
- No query: returns `/volumeX` roots
- `?path=/volume1`: returns subdirectories and parent

### `GET /api/history?limit=20`
Returns recent rename history.

## Sync API

### `POST /api/sync/profiles`
Create a new sync profile.

Request:
```json
{
  "name": "Backup Photos",
  "protocol": "local",
  "source_path": "/volume1/photos",
  "dest_path": "/volume2/backup",
  "source_user": "optional_user",
  "source_pass": "optional_pass",
  "dest_user": "optional_user",
  "dest_pass": "optional_pass",
  "recursive": true
}
```

Response includes profile ID and all fields.

### `GET /api/sync/profiles`
List all sync profiles.

Response:
```json
{
  "profiles": [
    {
      "id": "profile_uuid",
      "name": "Backup Photos",
      "protocol": "local",
      "source_path": "/volume1/photos",
      "dest_path": "/volume2/backup",
      "recursive": true,
      "created_at": "2026-03-28T10:15:00+00:00"
    }
  ]
}
```

### `POST /api/sync/run`
Execute a sync for a profile.

Request:
```json
{
  "profile_id": "profile_uuid"
}
```

Response:
```json
{
  "files_copied": 42,
  "files_skipped": 10,
  "errors": 0,
  "error_messages": []
}
```

### `POST /api/sync/delete-profile`
Delete a sync profile.

Request:
```json
{
  "profile_id": "profile_uuid"
}
```

## Build locally (Windows or Linux)

Run `python build.py` with Python 3. The script creates
`SynoDocFic-1.3.2-0004.spk`, checks the icon dimensions, normalizes shell
scripts to LF with executable permissions, and packages the canonical UI.

## Build `.spk` using Synology Package Toolkit

> Build commands are run on Linux (native, VM, or Docker) with Synology Package Toolkit installed.

1. Install Synology Package Toolkit and ensure `pkgscripts-ng` is available.
2. Place this project in your toolkit source workspace.
3. Ensure scripts are executable:

```bash
chmod +x scripts/*
chmod +x package/app/server.py
```

4. Build package:

```bash
pkgscripts-ng/PkgCreate.py -p <absolute-path-to-SynoDocFic>
```

5. Resulting `.spk` will be generated in the toolkit output directory.

## Install on DSM 7

1. Open DSM Package Center.
2. Click **Manual Install**.
3. Select generated `.spk`.
4. Complete install wizard.
5. Start package.
6. Open package from Package Center.

Version 1.3.2 opens an ExtJS DSM desktop window containing the existing web UI
in a same-origin iframe. The launcher uses `Ext.extend`, matching DSM's desktop
API, and declares its window dependency in `ui/config`.

DSM registers `target/ui` using `dsmuidir="ui"`. Install scripts do not write to
system web roots. Menu icons come from `ui/images/icon_{size}.png`; Package Center
uses separate 64px and 256px PNG files.

The `web-config` resource registers `package/nginx/api.conf` in the DSM nginx
server. Calls to `/webman/3rdparty/synobatchrenamer/api/` are forwarded to the
HTTP backend at `127.0.0.1:8765/api/`. The browser uses DSM's current HTTP/HTTPS
origin, without a separate certificate or a manually configured reverse proxy.
The package requests nginx reloads through INFO's service restart fields.
This resource requires DSM 7.2 or newer.

### Verification on the NAS

1. Install `SynoDocFic-1.3.2-0004.spk` through Package Center as an upgrade.
2. Sign out of DSM, sign in again, then refresh the browser cache (Ctrl+F5).
3. Check the SynoDocFic icon in the main menu and open the application.
4. Check that it opens inside the DSM desktop, can be resized, and can be closed
   and reopened without creating another browser tab.
5. Over DSM HTTPS, browse a folder and request a rename preview before applying
   any operation. In browser developer tools, API requests should use the DSM
   origin and `/webman/3rdparty/synobatchrenamer/api/`, never HTTPS port 8765.
6. If the icon stays blank, inspect the network request for
   `/webman/3rdparty/synobatchrenamer/images/icon_64.png` (status and response).
   If startup fails, inspect `/var/log/synopkg.log` for resource errors.

The launcher and nginx resource have been checked locally, but actual DSM 7.4.1
window behavior and package lifecycle still require validation on a NAS.
The proxy does not add DSM authentication to the existing backend API.

## Runtime Notes

- Service runs as package user (`sc-synobatchrenamer`), not root.
- Native DSM application UI: `package/app/static/` copied to `package/ui/` by
  `build.py` and exposed through `dsmuidir`.
- Backend API/UI default port: `8765`.
- Directory operations are restricted to absolute `/volumeX/...` paths.
- Sync feature:
  - Directly supports local-to-local paths.
  - Supports local, FTP and SMB/CIFS sources and destinations through the configured endpoint credentials.
  - SMB/CIFS browsing, comparison and copy require `smbclient` to be installed on DSM.
  - FTP browsing, comparison and copy use the Python FTP client.
  - Files are compared by size + mtime; identical files are not re-copied.
  - Recursive mode includes subdirectories; non-recursive copies files only from source root.
- Persisted runtime files:
  - History: `target/var/history.json`
  - Sync profiles: `target/var/sync_profiles.json`
  - Logs: `target/var/logs/operations.log` and `target/logs/service.log`

## Production Hardening Tips

- Put package behind DSM reverse proxy and HTTPS.
- Restrict network access by DSM firewall.
- Add DSM authentication integration or reverse-proxy auth if needed.
- Add automated tests for edge-case rename plans.
