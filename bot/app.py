import logging
from os import getenv, mkdir
from pathlib import Path

from dotenv import load_dotenv
from pyrogram.client import Client
from pyrogram.connection.transport.tcp.tcp import TCP
from pyrogram.session.session import Session

from bot.logsetup import configure_logging
from bot.version import APP_VERSION, DEVICE_MODEL

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

DEBUG = bool(getenv('DEBUG'))
configure_logging(CONFIG_FOLDER, debug=DEBUG)
logging.getLogger('pyrogram').setLevel(logging.INFO if DEBUG else logging.WARNING)
logging.getLogger('pyrogram.connection').setLevel(logging.WARNING)

MAX_SIMULTANEOUS_TRANSMISSIONS = max(1, int(getenv("MAX_CONCURRENT_DOWNLOADS", "3") or "3"))

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
            logging.info(
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
    logging.info('Using proxy %s', mask_proxy(PROXY))

app = Client(
    'TDownloader-bot',
    TAPI_ID,
    TAPI_HASH,
    bot_token=BOT_TOKEN,
    workdir=CONFIG_FOLDER,
    proxy=PROXY,
    device_model=DEVICE_MODEL,
    app_version=APP_VERSION,
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
        # 频道监听的新帖由用户账号接收，不能关更新，no_updates=True 会收不到任何消息
        device_model=DEVICE_MODEL,
        app_version=APP_VERSION,
        max_concurrent_transmissions=MAX_SIMULTANEOUS_TRANSMISSIONS,
    )
