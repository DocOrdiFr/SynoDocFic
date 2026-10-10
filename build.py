"""Build an unsigned DSM 7 SPK using only the Python standard library."""
import io
import json
import re
import struct
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def add(archive, name, data, mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = mode
    archive.addfile(entry, io.BytesIO(data))


def build():
    info = (ROOT / "INFO").read_text(encoding="utf-8")
    version = re.search(r'^version="([^"]+)"', info, re.M).group(1)
    config = json.loads((ROOT / "package/ui/config").read_text())
    appname = re.search(r'^dsmappname="([^"]+)"', info, re.M).group(1)
    assert appname in config["Main.js"], "DSM application name mismatch"
    resources = json.loads((ROOT / "conf/resource").read_text())
    for resource in resources["web-config"]["nginx-static-config"]["enable"]:
        assert (ROOT / "package" / resource["relpath"]).is_file()
    for path, size in [(ROOT / "PACKAGE_ICON.PNG", 64),
                       (ROOT / "PACKAGE_ICON_256.PNG", 256)] + [
        (ROOT / f"package/ui/images/icon_{size}.png", size)
        for size in (16, 24, 32, 48, 64, 72, 256)
    ]:
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size, size), path

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted((ROOT / "package").rglob("*")):
            if not path.is_file():
                continue
            name = path.relative_to(ROOT / "package").as_posix()
            if any(part in ("data", "logs", "__pycache__") for part in Path(name).parts):
                continue
            # One canonical web UI; keep desktop config, launcher and icons.
            source = ROOT / "package/app/static" / path.name if name in (
                "ui/index.html", "ui/app.js", "ui/styles.css") else path
            add(archive, name, source.read_bytes(), 0o755 if name == "app/server.py" else 0o644)

    output = ROOT / f"SynoDocFic-{version}.spk"
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as archive:
        add(archive, "INFO", info.replace("\r\n", "\n").encode())
        for name in ("PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG"):
            add(archive, name, (ROOT / name).read_bytes())
        add(archive, "package.tgz", payload.getvalue())
        for folder in ("conf", "scripts"):
            for path in sorted((ROOT / folder).iterdir()):
                if path.is_file():
                    add(archive, f"{folder}/{path.name}",
                        path.read_bytes().replace(b"\r\n", b"\n"),
                        0o755 if folder == "scripts" else 0o644)
    print(output)


if __name__ == "__main__":
    build()
