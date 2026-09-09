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
INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\n\r]+')
RENAME_PREFIXES = ("重命名:", "重命名：","重命名 ", "rename:", "name:")
KNOWN_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".3gp",
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus",
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".apk", ".exe", ".iso",
    ".txt", ".csv", ".json", ".xml", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".srt", ".ass", ".ssa", ".vtt", ".tgs",
}


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


def relative_name(filename: str) -> str:
    path = folder.getPath().replace("\\", "/").strip("/")
    if not path or path == ".":
        return filename
    return f"{path}/{filename}"


def has_extension(filename: str) -> bool:
    return len(os.path.splitext(filename)[1]) > 1


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


def looks_like_filename(text: str) -> bool:
    value = (text or "").strip().strip("\"'`")
    if not value or "\n" in value or len(value) > 180:
        return False
    if "://" in value or value.startswith("/"):
        return False
    if re.search(r"[。！？!?；;]", value):
        return False
    name = os.path.basename(value)
    stem, ext = os.path.splitext(name)
    if not stem or not ext:
        return False
    return ext.lower() in KNOWN_EXTENSIONS or 2 <= len(ext) <= 8


def looks_like_rename_name(text: str) -> bool:
    """判断文本是否像重命名；可以不带后缀。"""
    value = (text or "").strip().strip("\"'`")
    if not value or "\n" in value or len(value) > 80:
        return False
    if "://" in value or value.startswith("/"):
        return False
    if re.search(r"[。！？!?；;]", value):
        return False
    if looks_like_filename(value):
        return True
    name = os.path.basename(value)
    # 不带后缀的名字，例如「电影」「假期照片」
    if not name or name.endswith("."):
        return False
    _, ext = os.path.splitext(name)
    if ext:
        return False
    return True


def extract_rename_name(text: str | None) -> str | None:
    """从说明/独立文本里提取重命名。可不带后缀，也不必再加 >。"""
    if not text:
        return None
    first = text.strip().splitlines()[0].strip().strip("\"'`")
    if not first:
        return None
    forced = strip_rename_prefix(first)
    if forced is not None:
        return os.path.basename(forced) or None
    if looks_like_rename_name(first):
        return os.path.basename(first)
    return None


def usable_file_name(name: str | None) -> str | None:
    if not name:
        return None
    name = os.path.basename(name.strip())
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if GENERIC_NAME.match(stem) and len(ext) <= 1:
        return None
    return name


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
        filename = extract_rename_name(message.caption)
        if filename:
            filename = os.path.basename(filename)
    if not filename and media:
        filename = usable_file_name(getattr(media, "file_name", None))
    if not filename:
        filename = default_filename(kind, ext)
    elif not has_extension(filename) and ext:
        filename = f"{filename}{ext}"
    return filename


def sanitize_folder_name(name: str) -> str:
    cleaned = INVALID_FOLDER_CHARS.sub("_", name).strip(" .")
    return cleaned[:80]


def album_folder_name(messages: list[Message]) -> str:
    for message in messages:
        text = extract_rename_name(message.caption) or (message.caption or "").strip()
        text = sanitize_folder_name(text)
        if text:
            return text
    return datetime.now().strftime("相册_%Y-%m-%d_%H-%M-%S")


def unique_folder(name: str) -> str:
    base = folder.get()
    candidate = name
    index = 2
    while os.path.exists(os.path.join(base, candidate)):
        candidate = f"{name}_{index}"
        index += 1
    return candidate


def unique_filename(filename: str, directory: str, used: set[str]) -> str:
    filename = os.path.basename(filename)
    stem, ext = os.path.splitext(filename)
    candidate = filename
    index = 2
    while candidate.lower() in used or os.path.isfile(os.path.join(directory, candidate)):
        candidate = f"{stem}_{index}{ext}"
        index += 1
    used.add(candidate.lower())
    return candidate


def with_media_extension(name: str, message: Message) -> str:
    filename = os.path.basename((name or "").strip())
    if not filename:
        return filename
    if has_extension(filename):
        return filename
    media, kind = message_media(message)
    ext = extension_for(media, kind)
    return f"{filename}{ext}" if ext else filename


def replace_filename(path: str, filename: str) -> str:
    path = (path or "").replace("\\", "/")
    if "/" not in path:
        return filename
    return f"{path.rsplit('/', 1)[0]}/{filename}"
