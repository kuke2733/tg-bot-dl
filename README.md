
<p align="center">
  <a href="https://github.com/kuke2733/tg-bot-dl"><img src="https://img.shields.io/badge/GitHub-kuke2733%2Ftg--bot--dl-181717?logo=github&logoColor=white" alt="GitHub"></a>
  &nbsp;
  <a href="https://hub.docker.com/r/guyongbo/tg-bot-dl"><img src="https://img.shields.io/badge/DockerHub-guyongbo%2Ftg--bot--dl-2496ED?logo=docker&logoColor=white" alt="Docker Hub"></a>
</p>

一个基于 [Pyrogram] MTProto 框架开发的 **Telegram 文件下载机器人**。无需持续开启 Telegram 客户端或使用复杂的 CLI 工具，即可直接将 Telegram 中的文件（最高支持单文件 **4GB**）高速下载到你的服务器或本地存储中。

项目里每个文件的职责见 [STRUCTURE.md](STRUCTURE.md)。

---

## 🌟 核心特性

- 🚀 **大文件高速下载**：利用 Telegram MTProto 原生协议，打破普通 Bot API 的 20MB/50MB 限制，支持最大 **4GB** 单文件下载。
- 📊 **实时进度与速率反馈**：实时更新下载百分比、已下载大小、平均速率以及剩余预估时间（TTE）。
- 🛑 **随时取消下载**：下载通知自带内联取消按钮（Stop），误操作时可随时中断传输。
- 🔒 **受保护内容下载**：支持配置用户账号（User Client），可借助 `/add` 命令直接下载禁止转发/限制复制的私有频道或群组文件。
- 📁 **动态子目录管理**：支持通过指令随时切换保存的子目录，方便文件归类整理。
- 💾 **存储状态查询**：内置磁盘容量监控，随时通过命令检查服务器可用空间。
- 🛡️ **管理员鉴权机制**：支持配置白名单用户（用户名或数字 ID），防止未经授权的人员滥用。
- 🌐 **代理连接**：支持 HTTP / SOCKS5 代理，国内网络也可直连 Telegram。
- 🖥️ **可视化配置**：提供网页配置面板，在浏览器里改配置并立刻生效。

---

## 📋 前置准备

在部署前，请准备好以下信息：

1. **Telegram API ID & API Hash**：前往 [My Telegram] 官网，登录并进入 **API development tools** 创建应用获取。
2. **Bot Token**：通过 Telegram 官方 [@BotFather] 创建 Bot 并获取对应的 API Token。
3. **Telegram 手机号**（可选）：若需下载**禁止转发/受保护频道**的内容，需提供与有权限访问该内容的 Telegram 账号绑定的手机号（格式如 `+8613800000000`）。

---

## ⚙️ 环境变量配置

配置可以在网页面板里填写，保存后写入 `config/settings.env`。也可以用系统环境变量。

| 环境变量 | 是否必填 | 默认值 | 说明 |
| :--- | :---: | :---: | :--- |
| `TELEGRAM_API_ID` | **是** | - | 从 [My Telegram] 获取的 API ID（数字） |
| `TELEGRAM_API_HASH` | **是** | - | 从 [My Telegram] 获取的 API Hash（字符串） |
| `BOT_TOKEN` | **是** | - | 从 [@BotFather] 获取的机器人 Token |
| `ADMINS` | **是** | 空 | 管理员白名单，多个管理员用**空格**分隔。支持 `@用户名` 或 `用户ID`（例：`@username 12345678`） |
| `PHONE_NUMBER` | 否 | 空 | 用于辅助下载受限私有频道内容的用户手机号（需带国际区号，如 `+86...`） |
| `DOWNLOAD_FOLDER` | 否 | `/data` | 文件下载保存的主目录路径 |
| `CONFIG_FOLDER` | 否 | `/config` | Session 登录会话文件保存路径（建议持久化，避免容器重启后需重新登录） |
| `PROXY` | 否 | 空 | 访问 Telegram 的代理。支持 `socks5://127.0.0.1:7890`、`http://127.0.0.1:7890`；需要账号时写成 `socks5://user:pass@host:port`。只填 `127.0.0.1:7890` 时默认按 SOCKS5 处理 |
| `MAX_CONCURRENT_DOWNLOADS` | 否 | `6` | 同时下载的最大文件数 |
| `DEBUG` | 否 | 空 | 是否启用详细调试日志，设置任意非空值（如 `1`）开启 |

---

## 🚀 启动方式

本机安装 Python 3.13 后：

```bash
pip install -r requirements.txt
python start.py
```

浏览器打开 http://127.0.0.1:8080 ，填入 API ID、API Hash、Bot Token、管理员。需要代理就填，例如 `http://127.0.0.1:7890`。点「保存并生效」即可。

下载文件在 `data/`，登录会话和面板配置在 `config/`。如果填了手机号，验证码会直接出现在网页里。

### Docker

推荐直接用已发布镜像，新建一个目录后放入 `docker-compose.yml`：

```yaml
services:
  tg-bot-dl:
    image: guyongbo/tg-bot-dl:latest
    container_name: tg-bot-dl
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
      - ./config:/config
    environment:
      TZ: Asia/Shanghai
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

然后启动：

```bash
docker compose up -d
```

浏览器打开 http://127.0.0.1:8080 填写 API ID、API Hash、Bot Token 和管理员。下载文件保存在 `./data`，登录会话和面板配置保存在 `./config`。

容器里代理若填 `127.0.0.1`，会自动改写为宿主机地址 `host.docker.internal`。

本仓库已带同一份 `docker-compose.yml`。克隆源码后也可以本地构建：

```bash
docker compose up -d --build
```

也可以改用 GitHub Container Registry 镜像：`ghcr.io/kuke2733/tg-bot-dl:latest`。


---

## 🤖 机器人使用指南

### 1. 命令列表

| 指令 | 说明 |
| :--- | :--- |
| `/start` | 启动机器人并查看欢迎信息 |
| `/help` | 查看详细帮助与命令说明 |
| `/usage` | 查看当前下载目录所在磁盘的总容量、已用空间和剩余空间 |
| `/use 子路径` | 切换后续下载文件的存放子目录（如 `/use movies/action`）,注意空格分隔 |
| `/get` | 查看当前正在使用的下载目录相对路径 |
| `/leave` | 重置回根下载目录（`/`） |
| `/add 消息链接 重命名` | 从禁止转发/受限频道中通过消息链接下载文件,注意空格分隔 |

---

### 2. 文件发送与重命名规则

向机器人发送文档、视频、音频等任意媒体文件，机器人会自动加入下载队列。

重命名可以任选一种方式（**不用写后缀**，会自动补上）：

1. **转发附带名字**：先到名字、再到文件，自动套用
2. **回复下载进度消息**：回复进度消息，发新名字（如 `电影`）
3. **写在文件说明里**：说明里直接写 `电影`
4. **`/add` 命令**：`/add 链接 电影`

一次发送多个文件（相册）会保存到同一个文件夹；此时改名只改文件夹名，不改组内文件名。单个文件不建文件夹。

---

### 3. 下载受保护/禁止转发频道的文件

对于开启了“限制保存内容”（Restricted Content）的私有频道或群组，无法直接转发给 Bot，操作步骤如下：

1. 确保部署时已配置 `PHONE_NUMBER`，并且该账号已加入了目标私有频道；
2. 在频道中右键或长按目标消息，点击 **复制消息链接**（链接格式通常为 `https://t.me/c/1234567890/123`）；
3. 向机器人发送：
   ```text
   /add https://t.me/c/1234567890/123
   ```
   公开频道也可以：
   ```text
   /add https://t.me/频道用户名/123
   ```
   也可以同时指定保存的文件名（不用写后缀）：
   ```text
   /add https://t.me/c/1234567890/123 自定义名字
   ```

---

## 🧭 后续规划

### 下载核心
- [ ] **单文件多线程下载**：大文件按分片并行拉取再合并，提升单个文件下载速度（当前为顺序分片）
- [ ] **断点续传**：~~进程内失败续传~~（已支持网络中断后从 `.temp` 偏移继续）；仍缺：手动停止后保留、进程重启后恢复
- [x] **失败自动重试**：超时、限流等失败按次数退避重试（进程内，配合续传）
- [ ] **队列管理**：查看排队、取消未开始任务、暂停/继续、调整优先级
- [ ] **队列持久化**：进程重启后恢复未完成任务（配合断点续传）

### 批量与自动化
- [ ] **按范围批量下载**：`/add` 支持消息区间、话题整段，而不只是单条链接
- [ ] **频道监听自动下载**：加入频道后新媒体自动入队（可按类型/大小过滤）
- [ ] **跳过已存在文件**：同名+同大小或简单校验，避免重复占盘

### 体验与可控
- [ ] **限速**：避免打满带宽或触发 Telegram 限流
- [ ] **下载完成校验**：至少核对文件大小；可选更严的完整性检查
- [ ] **完成后动作**：解压、挪目录、Webhook/通知等可选钩子
- [ ] **网页查看队列与历史**：面板中查看任务列表与失败原因

---

## 📄 开源许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
