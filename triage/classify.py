"""Cheap local class + one-liner for a group. Not an LLM."""

from __future__ import annotations

from typing import Iterable

HARDWARE = (
    "fingerprint", "fprintd", "gpu", "nvidia", "amdgpu", "wifi", "wi-fi",
    "bluetooth", "framework", "trackpad", "tpm", "pam", "webcam", "audio",
    "jack", "hid",
)
UPDATE = ("upgrade", "quattro", "migrate", "update-to", "omarchy-upgrade")
DOCS = ("manual/", "readme", "docs/", ".md")
COSMETIC = ("theme", "font", "adwaita", "bar", "css", "wallpaper", "background")
JUNK = ("discord", "invite", "package-lock", "typo in readme")


def classify_group(titles: Iterable[str], paths: Iterable[str]) -> dict[str, str]:
    title = " ".join(titles).lower()
    path_blob = " ".join(paths).lower()
    blob = title + " " + path_blob
    if any(b in blob for b in JUNK) and not any(b in blob for b in HARDWARE + UPDATE):
        cls = "junk"
    elif any(b in blob for b in HARDWARE):
        cls = "hardware"
    elif any(b in blob for b in UPDATE):
        cls = "update-path"
    elif path_blob and all(
        p.endswith(".md") or p.startswith("docs/") or p.startswith("manual/")
        for p in paths
    ):
        cls = "docs"
    elif any(b in blob for b in COSMETIC):
        cls = "cosmetic"
    else:
        cls = "needs-look"
    note = next((t for t in titles if t), "")
    return {"card_class": cls, "card_note": note}
