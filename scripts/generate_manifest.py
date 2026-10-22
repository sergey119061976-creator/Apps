#!/usr/bin/env python3
"""Generate OTA manifest, app icon and page metadata from a release IPA."""

import argparse
import json
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zipfile
import plistlib
from pathlib import Path
from PIL import Image


REPOSITORY = "sergey119061976-creator/Apps"


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "GitHub-Actions"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as image:
            header = image.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return None
        dimensions = struct.unpack(">II", header[16:24])
        # iOS CgBI PNGs are not browser-ready; only accept images Pillow can decode.
        with Image.open(path) as image:
            image.verify()
        return dimensions
    except OSError:
        return None


def icon_candidates_from_plist(info: dict, app_prefix: str) -> list[str]:
    names: list[str] = []
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        icons = info.get(key, {})
        primary = icons.get("CFBundlePrimaryIcon", {}) if isinstance(icons, dict) else {}
        files = primary.get("CFBundleIconFiles", []) if isinstance(primary, dict) else []
        names.extend(f"{app_prefix}/{name}" for name in files if isinstance(name, str))
    return names


def extract_icon(archive: zipfile.ZipFile, info: dict, app_dir: str, work: Path) -> Path:
    """Prefer original icon PNGs; otherwise decode AppIcon from Assets.car."""
    names = icon_candidates_from_plist(info, app_dir)
    exact_icon_paths = [
        name for name in archive.namelist()
        if name.startswith(app_dir + "/")
        and Path(name).suffix.lower() == ".png"
        and ("appicon" in Path(name).name.lower() or "icon" in Path(name).name.lower())
    ]
    names.extend(exact_icon_paths)
    for name in dict.fromkeys(names):
        try:
            payload = archive.read(name)
        except KeyError:
            continue
        candidate = work / "candidate.png"
        candidate.write_bytes(payload)
        size = png_size(candidate)
        if size and size[0] == size[1] and size[0] >= 120:
            result = work / "app-icon.png"
            shutil.copyfile(candidate, result)
            return result

    car_paths = [
        name for name in archive.namelist()
        if name.startswith(app_dir + "/") and name.endswith("/Assets.car")
    ]
    if not car_paths:
        raise RuntimeError("IPA contains no usable icon PNG and no Assets.car")

    car_file = work / "Assets.car"
    car_file.write_bytes(archive.read(car_paths[0]))
    extractor = shutil.which("carfile")
    if not extractor:
        raise RuntimeError("carfile-go CLI is missing from PATH")
    extracted = work / "car-extracted"
    subprocess.run(
        [extractor, "--output", str(extracted), "--include", "AppIcon", str(car_file)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    pngs = []
    for path in extracted.rglob("*.png"):
        size = png_size(path)
        if size and size[0] == size[1]:
            pngs.append((size[0], path))
    if not pngs:
        raise RuntimeError("carfile-go extracted no square AppIcon PNG rendition")
    # The largest square rendition is the best available source for the web icon.
    _, best = max(pngs, key=lambda pair: pair[0])
    result = work / "app-icon.png"
    shutil.copyfile(best, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    event_path = Path(args.event)
    event = json.loads(event_path.read_text(encoding="utf-8"))
    # Release events wrap data under "release"; API lookups return it at the root.
    release = event.get("release", event)
    tag = release["tag_name"]
    ipa_assets = [
        asset for asset in release.get("assets", [])
        if asset.get("name", "").lower().endswith(".ipa")
    ]
    if len(ipa_assets) != 1:
        raise RuntimeError("Release must contain exactly one IPA file")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ios-release-") as temp:
        work = Path(temp)
        ipa_path = work / "app.ipa"
        download(ipa_assets[0]["browser_download_url"], ipa_path)
        with zipfile.ZipFile(ipa_path) as archive:
            # An IPA can bundle Watch apps and extensions with their own Info.plist.
            # The installable iOS app is the .app directly inside Payload/.
            plist_paths = [
                name for name in archive.namelist()
                if name.startswith("Payload/")
                and name.endswith(".app/Info.plist")
                and len(name.split("/")) == 3
            ]
            if len(plist_paths) != 1:
                raise RuntimeError(
                    "IPA must contain exactly one main Payload/<App>.app/Info.plist; "
                    f"found {len(plist_paths)}: {plist_paths}"
                )
            plist_path = plist_paths[0]
            app_dir = plist_path.removesuffix("/Info.plist")
            info = plistlib.loads(archive.read(plist_path))
            bundle_id = info.get("CFBundleIdentifier")
            version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
            title = info.get("CFBundleDisplayName") or info.get("CFBundleName")
            if not bundle_id or not version or not title:
                raise RuntimeError("Could not determine app bundle ID, version, and title")
            icon_source = extract_icon(archive, info, app_dir, work)
            shutil.copyfile(icon_source, output_dir / "app-icon.png")

        ipa_url = ipa_assets[0]["browser_download_url"]
        manifest = {
            "items": [{
                "assets": [{"kind": "software-package", "url": ipa_url}],
                "metadata": {
                    "bundle-identifier": bundle_id,
                    "bundle-version": str(version),
                    "kind": "software",
                    "title": title,
                },
            }]
        }
        with (output_dir / "manifest.plist").open("wb") as output:
            plistlib.dump(manifest, output, fmt=plistlib.FMT_XML, sort_keys=False)

    manifest_url = (
        f"https://github.com/{REPOSITORY}/releases/download/{tag}/manifest.plist"
    )
    app_info = {
        "title": title,
        "version": str(version),
        "tag": tag,
        "icon": "app-icon.png",
        "manifest": manifest_url,
        "download": ipa_url,
    }
    (output_dir / "app-info.json").write_text(
        json.dumps(app_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated release files for {title} {version} ({tag})")


if __name__ == "__main__":
    main()
