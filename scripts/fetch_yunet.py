"""Download the YuNet face model the privacy guard needs.

Run this once, deliberately::

    .venv/Scripts/python.exe scripts/fetch_yunet.py

The model is not committed to the repository. A 230 KB binary blob in a
source tree is something nobody reads, nobody diffs and nobody audits, so
it is fetched on request instead, and the download is checked against a
known hash before it is written anywhere the island will load it from.

The island never downloads this on its own. A background overlay that
reaches out to the internet unprompted is exactly the behaviour this
project is supposed to be the opposite of.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import paths
from core.vision import YUNET_FILENAME

URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
# Verified against the file published by the OpenCV Zoo project.
EXPECTED_SHA256 = (
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
)
EXPECTED_BYTES = 232_589


def target_directory(user_scope: bool) -> Path:
    """Where to put the model.

    The user profile by default, so an installed copy under Program Files
    never needs write access and an upgrade cannot delete the model.
    """
    directory = (paths.user_dir() / "models") if user_scope else (ROOT / "models")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def download(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "adaptive-island"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main() -> int:
    user_scope = "--repo" not in sys.argv
    directory = target_directory(user_scope)
    destination = directory / YUNET_FILENAME

    if destination.is_file():
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest == EXPECTED_SHA256:
            print(f"Already present and verified: {destination}")
            return 0
        print(f"Existing file at {destination} has an unexpected hash; replacing.")

    print(f"Downloading YuNet ({EXPECTED_BYTES:,} bytes)")
    print(f"  from {URL}")
    try:
        payload = download(URL)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"\nDownload failed: {exc}")
        print("\nIf this machine is offline, fetch the file elsewhere and put it at:")
        print(f"  {destination}")
        return 1

    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256:
        # Refuse rather than warn. This file is loaded into the process
        # that watches the camera; a mismatch is a stop condition.
        print("\nHash mismatch. Nothing was written.")
        print(f"  expected {EXPECTED_SHA256}")
        print(f"  received {digest}")
        return 2

    # Write to a temporary file and move it into place, so an interrupted
    # download cannot leave a half-written model for the guard to load.
    with tempfile.NamedTemporaryFile(
        dir=str(directory), prefix=".yunet-", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    shutil.move(str(temporary), str(destination))

    print(f"\nVerified and saved to {destination}")
    print(f"  sha256 {digest}")
    print("\nThe privacy guard can now be enabled from the tray menu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
