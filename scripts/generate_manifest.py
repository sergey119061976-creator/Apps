
import argparse
import json
import os
import plistlib
import urllib.request
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ipa-output", required=True)
    args = parser.parse_args()

    with open(args.event, encoding="utf-8") as f:
        event = json.load(f)

    release = event["release"]
    tag = release["tag_name"]

    assets = [
        asset for asset in release.get("assets", [])
        if asset["name"].lower().endswith(".ipa")
    ]

    if len(assets) != 1:
        raise RuntimeError(
            "В релизе должен находиться ровно один IPA-файл."
        )

    ipa_asset = assets[0]
    ipa_url = ipa_asset["browser_download_url"]

    os.makedirs(os.path.dirname(args.ipa_output), exist_ok=True)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    request = urllib.request.Request(
        ipa_url,
        headers={"User-Agent": "GitHub-Actions"}
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        with open(args.ipa_output, "wb") as ipa_file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                ipa_file.write(chunk)

    with zipfile.ZipFile(args.ipa_output) as archive:
        info_paths = [
            name for name in archive.namelist()
            if name.startswith("Payload/")
            and name.endswith(".app/Info.plist")
        ]

        if not info_paths:
            raise RuntimeError(
                "В IPA не найден Payload/*.app/Info.plist"
            )

        info_paths.sort(key=len)
        info = plistlib.loads(archive.read(info_paths[0]))

    bundle_id = info.get("CFBundleIdentifier")
    version = info.get("CFBundleVersion")
    title = (
        info.get("CFBundleDisplayName")
        or info.get("CFBundleName")
    )

    if not version:
        version = info.get("CFBundleShortVersionString")

    if not bundle_id or not version or not title:
        raise RuntimeError(
            "Не удалось определить Bundle ID, версию или название."
        )

    manifest = {
        "items": [
            {
                "assets": [
                    {
                        "kind": "software-package",
                        "url": ipa_url,
                    }
                ],
                "metadata": {
                    "bundle-identifier": bundle_id,
                    "bundle-version": version,
                    "kind": "software",
                    "title": title,
                },
            }
        ]
    }

    with open(args.output, "wb") as f:
        plistlib.dump(
            manifest,
            f,
            fmt=plistlib.FMT_XML,
            sort_keys=False,
        )

    print(f"Release: {tag}")
    print(f"App: {title}")
    print(f"Bundle ID: {bundle_id}")
    print(f"Version: {version}")
    print(f"IPA: {ipa_url}")
    print(f"Manifest: {args.output}")


if __name__ == "__main__":
    main()
