from __future__ import annotations

import os
import re
from datetime import datetime
from mimetypes import guess_extension

from pyrogram.types import Message

from bot import folder


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/mkv": ".mkv",
    "video/x-msvideo": ".avi",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-tgsticker": ".tgs",
    "application/vnd.android.package-archive": ".apk",
}

KIND_FALLBACK_EXTENSIONS = {
    "photo": ".jpg",
    "live_photo": ".jpg",
    "video": ".mp4",
    "animation": ".mp4",
    "audio": ".mp3",
    "voice": ".ogg",
    "video_note": ".mp4",
    "sticker": ".webp",
}

KIND_PREFIX = {
    "photo": "photo",
    "live_photo": "photo",
    "video": "video",
    "animation": "animation",
    "audio": "audio",
    "voice": "voice",
    "video_note": "video_note",
    "sticker": "sticker",
    "document": "file",
}

GENERIC_NAME = re.compile(r"^File-\d+$", re.I)
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\n\r]+')
METADATA_LINE = re.compile(
    r"(?i)^(artist|album|title|duration|size|type|genre|year|track|performer|date)\s*[:：]"
)
RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
RENAME_PREFIXES = ("重命名:", "重命名：","重命名 ", "rename:", "name:")
# Linux 单节 NAME_MAX 一般是 255 字节；Windows 单节是 255 个 UTF-16 码元。
# 两边都能过：按 UTF-8 255 字节截，并预留 _{n} 给去重。
MAX_NAME_BYTES = 255
UNIQUE_SUFFIX_RESERVE = 8
URL_IN_TEXT = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.I)


def message_media(message: Message):
    if not message.media:
        return None, ""
    kind = message.media.value
    return getattr(message, kind, None), kind


def media_file_size(message: Message) -> int:
    media, _ = message_media(message)
    try:
        return int(getattr(media, "file_size", 0) or 0)
    except Exception:
        return 0


def media_file_unique_id(message: Message) -> str:
    media, _ = message_media(message)
    return str(getattr(media, "file_unique_id", "") or "")


def has_extension(filename: str) -> bool:
    return len(os.path.splitext(filename)[1]) > 1


def _collapse_name(name: str) -> str:
    name = INVALID_NAME_CHARS.sub("_", name)
    name = re.sub(r"\s*_\s*", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip(" ._")


def _clip_utf8_bytes(text: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip(" ._")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def sanitize_filename(name: str) -> str:
    name = _collapse_name(os.path.basename((name or "").strip()))
    if not name:
        return ""
    stem, ext = os.path.splitext(name)
    if not stem:
        return ""
    if stem.upper() in RESERVED_FILENAMES:
        stem += "_"
    limit = MAX_NAME_BYTES - UNIQUE_SUFFIX_RESERVE
    ext_bytes = len(ext.encode("utf-8"))
    stem = _clip_utf8_bytes(stem, max(1, limit - ext_bytes))
    return f"{stem}{ext}" if stem else ""


def flatten_caption(text: str | None) -> str:
    """去掉链接、#、换行，空白压成单空格，其余尽量保留。"""
    value = URL_IN_TEXT.sub(" ", str(text or ""))
    value = value.replace("#", "")
    return " ".join(value.split()).strip(" \"'`")


def caption_is_metadata(text: str | None) -> bool:
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    hits = sum(1 for line in lines if METADATA_LINE.match(line))
    return hits >= 2


def audio_display_name(media, ext: str) -> str | None:
    title = os.path.splitext(sanitize_filename(str(getattr(media, "title", "") or "")))[0]
    performer = os.path.splitext(sanitize_filename(str(getattr(media, "performer", "") or "")))[0]
    if performer and title:
        return sanitize_filename(f"{performer} - {title}{ext}")
    if title:
        return sanitize_filename(f"{title}{ext}")
    return None


def extension_for(media, kind: str) -> str:
    mime = str(getattr(media, "mime_type", "") or "").lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    guessed = guess_extension(mime) if mime else None
    if guessed in (".jpe", ".jpeg"):
        return ".jpg"
    if guessed in (".oga", ".ogx"):
        return ".ogg"
    if guessed:
        return guessed
    if mime:
        return ""
    return KIND_FALLBACK_EXTENSIONS.get(kind, "")


def default_filename(kind: str, ext: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    prefix = KIND_PREFIX.get(kind, "file")
    return f"{prefix}_{stamp}{ext}"


def strip_rename_prefix(text: str) -> str | None:
    value = text.strip()
    lower = value.lower()
    for prefix in RENAME_PREFIXES:
        if lower.startswith(prefix.lower()):
            name = value[len(prefix):].strip()
            return name or None
    return None


def extract_caption_filename(text: str | None) -> str | None:
    """媒体说明和回复改名同一套：去链接、#、换行，其余尽量留，超长再截。"""
    if not text:
        return None
    if caption_is_metadata(text):
        return None
    flattened = flatten_caption(text)
    if not flattened or flattened.startswith("/"):
        return None
    forced = strip_rename_prefix(flattened)
    if forced is not None:
        flattened = flatten_caption(forced)
        if not flattened:
            return None
    return sanitize_filename(_collapse_name(flattened)) or None


def extract_rename_name(text: str | None) -> str | None:
    """回复改名、先名字后文件，与说明取名同一套清洗。"""
    return extract_caption_filename(text)


def usable_file_name(name: str | None) -> str | None:
    if not name:
        return None
    name = os.path.basename(name.strip())
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if GENERIC_NAME.match(stem) and len(ext) <= 1:
        return None
    return sanitize_filename(name) or None


def resolve_filename(
    message: Message,
    override: str | None = None,
    use_caption_name: bool = True,
) -> str:
    media, kind = message_media(message)
    ext = extension_for(media, kind)
    filename = (override or "").strip() or None
    if filename:
        filename = os.path.basename(filename)
    if not filename and use_caption_name:
        filename = extract_caption_filename(message.caption)
    if not filename and media:
        filename = usable_file_name(getattr(media, "file_name", None))
    if not filename and media and kind == "audio":
        filename = audio_display_name(media, ext)
    if not filename:
        filename = default_filename(kind, ext)
    filename = sanitize_filename(filename) or default_filename(kind, ext)
    if not has_extension(filename) and ext:
        filename = sanitize_filename(f"{os.path.splitext(filename)[0]}{ext}") or default_filename(kind, ext)
    return filename


def sanitize_folder_name(name: str) -> str:
    return _clip_utf8_bytes(_collapse_name(name), MAX_NAME_BYTES - UNIQUE_SUFFIX_RESERVE)


def album_folder_name(messages: list[Message]) -> str:
    for message in messages:
        text = extract_caption_filename(message.caption)
        if not text and not caption_is_metadata(message.caption):
            text = flatten_caption(message.caption)
        text = sanitize_folder_name(text or "")
        if text:
            return text
    return datetime.now().strftime("相册_%Y-%m-%d_%H-%M-%S")


def unique_folder(name: str, base: str | None = None) -> str:
    base = base if base is not None else folder.get()
    candidate = name
    index = 2
    while os.path.exists(os.path.join(base, candidate)):
        candidate = f"{name}_{index}"
        index += 1
    return candidate


def unique_filename(filename: str, directory: str, used: set[str]) -> str:
    filename = sanitize_filename(os.path.basename(filename)) or "file"
    stem, ext = os.path.splitext(filename)
    candidate = filename
    index = 2
    while candidate.lower() in used or os.path.isfile(os.path.join(directory, candidate)):
        candidate = f"{stem}_{index}{ext}"
        index += 1
    used.add(candidate.lower())
    return candidate


def with_media_extension(name: str, message: Message) -> str:
    filename = sanitize_filename(os.path.basename((name or "").strip()))
    if not filename:
        return filename
    if has_extension(filename):
        return filename
    media, kind = message_media(message)
    ext = extension_for(media, kind)
    return sanitize_filename(f"{filename}{ext}") if ext else filename


def replace_filename(path: str, filename: str) -> str:
    path = (path or "").replace("\\", "/")
    if "/" not in path:
        return filename
    return f"{path.rsplit('/', 1)[0]}/{filename}"
