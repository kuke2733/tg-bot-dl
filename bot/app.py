import logging
from os import getenv, mkdir
from pathlib import Path

from dotenv import load_dotenv
from pyrogram.client import Client
from pyrogram.connection.transport.tcp.tcp import TCP
from pyrogram.session.session import Session

# 走代理时默认 2 秒超时太短，会话会不停重启，文件消息就收不到
Session.START_TIMEOUT = 20
Session.WAIT_TIMEOUT = 60
TCP.TIMEOUT = 30


def running_in_docker() -> bool:
    return Path('/.dockerenv').exists() or getenv('IN_DOCKER') == '1'


def default_folders():
    root = Path(__file__).resolve().parent.parent
    if running_in_docker():
        return getenv('DOWNLOAD_FOLDER', '/data'), getenv('CONFIG_FOLDER', '/config')
    return (
        getenv('DOWNLOAD_FOLDER', str(root / 'data')),
        getenv('CONFIG_FOLDER', str(root / 'config')),
    )


load_dotenv()
BASE_FOLDER, CONFIG_FOLDER = default_folders()
load_dotenv(Path(CONFIG_FOLDER) / 'settings.env')
BASE_FOLDER, CONFIG_FOLDER = default_folders()
DL_FOLDER = BASE_FOLDER

# 配置日志
log_level = logging.INFO if getenv('DEBUG') else logging.WARN
logging.basicConfig(
    level=log_level,
    format='%(levelname)s:%(name)s:%(message)s',
    handlers=[logging.StreamHandler()]
)

# 确保日志使用 UTF-8 编码
import sys
for handler in logging.root.handlers:
    if hasattr(handler, 'stream') and hasattr(handler.stream, 'reconfigure'):
        try:
            handler.stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

MAX_SIMULTANEOUS_TRANSMISSIONS = max(1, int(getenv("MAX_CONCURRENT_DOWNLOADS", "6") or "6"))

ADMINS = getenv('ADMINS', '').split()
BOT_TOKEN = getenv('BOT_TOKEN')
PHONE_NUMBER = getenv('PHONE_NUMBER')
TAPI_HASH = getenv('TELEGRAM_API_HASH')

try:
    TAPI_ID = int(getenv('TELEGRAM_API_ID', '') or '0')
except ValueError:
    logging.error('TELEGRAM_API_ID must be a number')
    raise SystemExit(1)


def mask_proxy(value: str) -> str:
    if '@' in value:
        scheme, rest = value.split('://', 1)
        _, host = rest.rsplit('@', 1)
        return f'{scheme}://***@{host}'
    return value


def parse_proxy(value: str | None):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if '://' not in value:
        value = f'socks5://{value}'
    if running_in_docker():
        rewritten = (
            value.replace('127.0.0.1', 'host.docker.internal')
            .replace('localhost', 'host.docker.internal')
        )
        if rewritten != value:
            logging.warning(
                'Docker detected, proxy rewritten to %s',
                mask_proxy(rewritten),
            )
        value = rewritten
    return value


PROXY = parse_proxy(getenv('PROXY'))

for path in [DL_FOLDER, CONFIG_FOLDER]:
    try:
        mkdir(path)
    except FileExistsError:
        pass
    except Exception:
        logging.error('Failed to create %s folder!', path)
        raise SystemExit(1)

if PROXY:
    logging.warning('Using proxy %s', mask_proxy(PROXY))

app = Client(
    'TDownloader-bot',
    TAPI_ID,
    TAPI_HASH,
    bot_token=BOT_TOKEN,
    workdir=CONFIG_FOLDER,
    proxy=PROXY,
    max_concurrent_transmissions=MAX_SIMULTANEOUS_TRANSMISSIONS,
)

user = None
if PHONE_NUMBER:
    user = Client(
        'TDownloader-user',
        TAPI_ID,
        TAPI_HASH,
        phone_number=PHONE_NUMBER,
        workdir=CONFIG_FOLDER,
        proxy=PROXY,
        no_updates=True,
        max_concurrent_transmissions=MAX_SIMULTANEOUS_TRANSMISSIONS,
    )
