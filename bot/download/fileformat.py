from __future__ import annotations

import logging
from pathlib import Path


MEDIA_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".m4a",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".ts",
    ".mts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".3g2",
}


def sniff_extension(path: str | Path) -> str | None:
    try:
        with Path(path).open("rb") as fh:
            header = fh.read(4096)
    except OSError:
        return None
    if len(header) < 12:
        return None

    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand == b"qt  ":
            return ".mov"
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return ".m4a"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return ".heic"
        if brand == b"avif":
            return ".avif"
        return ".mp4"

    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm" if b"webm" in header else ".mkv"
    if header.startswith(b"%PDF"):
        return ".pdf"
    if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
        return ".zip"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG"):
        return ".png"
    if header.startswith(b"GIF8"):
        return ".gif"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"ID3"):
        return ".mp3"
    if header.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if header.startswith(b"7z\xbc\xaf'\x1c"):
        return ".7z"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return ".avi"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def align_filename_to_container(saved_path: str, relative_name: str) -> tuple[str, str]:
    detected = sniff_extension(saved_path)
    if not detected:
        return saved_path, relative_name

    path = Path(saved_path)
    current_ext = path.suffix.lower()
    if current_ext == detected.lower():
        return saved_path, relative_name
    if current_ext and current_ext not in MEDIA_EXTENSIONS:
        return saved_path, relative_name

    new_path = path.with_suffix(detected)
    if new_path.exists():
        logging.info("未改后缀，目标已存在：%s", new_path.name)
        return saved_path, relative_name

    path.rename(new_path)
    new_rel = str(Path(relative_name).with_suffix(detected)).replace("\\", "/")
    logging.info("按真实格式调整后缀：%s -> %s", relative_name, new_rel)
    return str(new_path), new_rel
