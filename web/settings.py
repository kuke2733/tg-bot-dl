from pathlib import Path
from os import getenv, mkdir

FIELDS = [
    ("TELEGRAM_API_ID", "API ID", "text", True),
    ("TELEGRAM_API_HASH", "API Hash", "password", True),
    ("BOT_TOKEN", "Bot Token", "password", True),
    ("ADMINS", "管理员", "text", True),
    ("PROXY", "代理", "text", False),
    ("PHONE_NUMBER", "手机号", "text", False),
    ("DEBUG", "调试日志", "checkbox", False),
    ("WEB_PASSWORD", "面板密码", "password", False),
]

BOT_KEYS = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "BOT_TOKEN",
    "ADMINS",
    "PROXY",
    "PHONE_NUMBER",
    "DEBUG",
    "DOWNLOAD_FOLDER",
    "CONFIG_FOLDER",
    "IN_DOCKER",
    "MAX_CONCURRENT_DOWNLOADS",
]


def running_in_docker() -> bool:
    return Path("/.dockerenv").exists() or getenv("IN_DOCKER") == "1"


def default_folders():
    if running_in_docker():
        download = getenv("DOWNLOAD_FOLDER", "/data")
        config = getenv("CONFIG_FOLDER", "/config")
    else:
        root = Path(__file__).resolve().parent.parent
        download = getenv("DOWNLOAD_FOLDER", str(root / "data"))
        config = getenv("CONFIG_FOLDER", str(root / "config"))
    for folder in (download, config):
        try:
            mkdir(folder)
        except FileExistsError:
            pass
        except FileNotFoundError:
            Path(folder).mkdir(parents=True, exist_ok=True)
    return download, config


def settings_path() -> Path:
    _, config = default_folders()
    return Path(config) / "settings.env"


def parse_env(text: str) -> dict[str, str]:
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def load() -> dict[str, str]:
    data = {}
    for key, *_ in FIELDS:
        env_value = getenv(key)
        if env_value:
            data[key] = env_value
    path = settings_path()
    if path.exists():
        data.update(parse_env(path.read_text(encoding="utf-8", errors="ignore")))
    download, config = default_folders()
    data["DOWNLOAD_FOLDER"] = getenv("DOWNLOAD_FOLDER", download)
    data["CONFIG_FOLDER"] = getenv("CONFIG_FOLDER", config)
    if running_in_docker():
        data["IN_DOCKER"] = "1"
    return data


def save(values: dict[str, str]) -> dict[str, str]:
    current = load()
    for key, *_rest in FIELDS:
        if key in values:
            current[key] = (values.get(key) or "").strip()
    download, config = default_folders()
    current["DOWNLOAD_FOLDER"] = getenv("DOWNLOAD_FOLDER", download)
    current["CONFIG_FOLDER"] = getenv("CONFIG_FOLDER", config)
    lines = []
    for key, *_rest in FIELDS:
        value = current.get(key, "")
        if value:
            lines.append(f"{key}={value}")
    path = settings_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load()


def missing_required(data: dict[str, str]) -> list[str]:
    missing = []
    for key, label, _kind, required in FIELDS:
        if required and not (data.get(key) or "").strip():
            missing.append(label)
    return missing


def bot_env(data: dict[str, str]) -> dict[str, str]:
    env = {}
    for key in BOT_KEYS:
        value = (data.get(key) or "").strip()
        if value:
            env[key] = value
    return env
