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

Version 1.3 provides a native DSM application entry point. The button created by
Package Center opens the complete application directly in the DSM window:
there is no intermediate launcher page and no iframe.

The DSM desktop entry is declared by `dsmappname` in `INFO` and the
`package/ui/config` file. After installing or upgrading the package, DSM may
need a logout/login or a browser refresh before the **SynoDocFic** icon appears
in the main menu, just as for other DSM applications.

The DSM desktop integration uses a real `type: "app"` definition and
`package/ui/Main.js`. DSM creates the application window and embeds the local
package service inside it; it is not launched as a separate browser tab.

The package daemon still listens on its private local HTTP endpoint (`8765`) for
the API. This is an internal service port and is not declared as `adminport`;
the DSM application uses it automatically;
the user does not need to open it or enter it manually. When the UI is opened
from DSM over HTTPS, the package API must be exposed through a DSM HTTPS
reverse proxy to avoid mixed-content blocking, or DSM must be configured to
allow the package's local endpoint.

## Runtime Notes

- Service runs as package user (`sc-synobatchrenamer`), not root.
- Native DSM application UI: `package/app/static/` copied to `package/ui/` by
  `build.sh` and exposed through `dsmuidir`.
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
