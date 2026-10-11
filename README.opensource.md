# 关鹏网与关鹏机器人开源傻瓜包

[下载 Windows / Linux 部署包](https://github.com/shuiwen879867/GuanpengNet/releases/latest) · [Linux 测试记录](https://github.com/shuiwen879867/GuanpengNet/actions/workflows/linux-test.yml)

本项目包含关鹏网战绩查询网站，以及基于 AstrBot 4.14.4 的关鹏机器人（战绩、职业统计、签到和背景）。采用 AGPL-3.0，保留 AstrBot 的许可证和完整框架源码。

## Windows 三步启动

1. 安装 Python 3.10+（本机测试版本为 3.13），安装时勾选 Add Python to PATH。机器人图片功能需要安装 Chrome 或 Edge。
2. 解压到普通文件夹。双击 `start-web.bat` 启动网站，`start-bot.bat` 启动机器人，或 `start-all.bat` 启动包内全部服务。首次自动创建虚拟环境、安装依赖和生成 `config.json`，需要联网。
3. 网站打开 `http://127.0.0.1:5000`；机器人面板打开 `http://127.0.0.1:6185`。机器人账号为 `astrbot`，首次随机密码保存在 `runtime/bot-login.txt`。在面板配置自己的消息平台连接，QQ 连接需要自行配置对应平台适配器。

Domcer 战绩接口需要你自己的 API Key。进入 **Domcer 大厅，在游戏聊天中输入 `/key`** 获取密钥，然后在生成的 `config.json` 中填写 `"domcer_api_keys": ["你的密钥"]` 并重启。密钥请自行保管，不要提交到 GitHub。没有密钥也可以启动和浏览网站，战绩查询会提示配置。包内没有原作者的密钥、QQ 连接、绑定、聊天记录或比赛数据。

第一次打开浏览器时服务可能仍在初始化，稍等后刷新即可。关闭启动窗口或按 Ctrl+C 停止。再次启动会复用环境，依赖列表不变时跳过安装。配置格式错误、端口占用或安装失败会显示错误并保留窗口。

## 独立部署与可选联动

每个平台分别提供 `GuanpengNet-Windows.zip` / `GuanpengNet-Linux.zip`（网站），`GuanpengBot-Windows.zip` / `GuanpengBot-Linux.zip`（机器人），以及 `GuanpengSuite-Windows.zip` / `GuanpengSuite-Linux.zip`（合并包）。两者互不调用对方服务，单独运行时自行更新统计。

`config.json` 的 `linkage_enabled` 默认为 `true`。开启时，两者通过 `shared_data_dir` 共用对局数据库及统计缓存，默认目录为 `runtime/shared`。网站和机器人可同时追加队列，文件锁防止覆盖；接口失败的对局会保留待重试。

改为 `false` 并重启后，网站使用 `web_data_dir`（默认 `runtime/web`），机器人使用 `bot_data_dir`（默认 `runtime/bot`），完全独立。切换开关不会自动迁移历史数据，原目录仍保留。机器人账号绑定、签到余额及图片始终属于机器人自身，不向网站共享。

两个独立包解压到不同目录时，即使联动开启，默认目录也不会指向同一个地方。要在同一台电脑联动，将两边的 `shared_data_dir` 设置成同一个绝对目录。跨电脑不会自动同步，需自行提供支持文件锁的共享存储。

网站和机器人端口分别由 `web_port`、`bot_dashboard_port` 控制，默认只监听本机。需要远程访问时可自行设置 `web_host` 和 `bot_dashboard_host`。`game_status_url` 留空则不连接对战器；填自己的接口根地址可启用游戏状态查询。

## 指令与配置

常用指令：`/mw查询 玩家名`、`/mw详情 比赛ID`、`/职业查询`、`/昨日职业数据`、`/昨日表现 玩家名`、`/今日表现 玩家名`、`/绑定 游戏ID`、`/解绑`、`/签到`、`/关鹏币`、`/关鹏help`。

功能指令名称放在 `shared_data/commands_guanpeng.json`，不再在注册代码中写死。也可以在 `config.json` 的 `commands` 中按处理函数名覆盖，例如 `"commands": {"query_megawalls": "战绩"}`，重启生效。命令名称须唯一且不含空格；修改名称后说明图片中的示例文字仍使用默认名字。

已删除私人玩家页面、定向消息转发、固定用户/群号特权、发币和强制修改等插件管理员指令、查询屏蔽/贬损名单及私人老婆系统。网站开源版不包含广告购买、爱发电回调、留言后台和管理重建接口。AstrBot 框架的内置聊天指令默认禁用，面板保留用于配置平台连接。

## Linux / 手动部署

下载文件名带 `Linux` 的包。Debian/Ubuntu 可执行 `sh install-linux.sh web`（或 `bot` / `all`）安装 Python、虚拟环境工具，以及机器人需要的 Chromium、中文字体等系统依赖，然后自动启动。非 root 用户通过 sudo 输入系统密码。其他发行版请先安装对应依赖，再运行下面的启动脚本。网站包使用 `all` 时仅启动网站，但安装脚本可能额外安装浏览器；直接选择 `web` 可避免这一点。

```sh
sh start.sh web
sh start.sh bot
sh start.sh all
```

Linux 图片渲染需安装 Chromium 和中文字体，并确保 `html2image` 能找到浏览器。已在 GitHub Actions 的 Ubuntu 24.04 / Python 3.13 环境实际验证网站、机器人、合并部署、面板登录和 Chrome 战绩图片生成。

Linux 提供 `start-web.sh`、`start-bot.sh`、`start-all.sh`，可通过 `sh 文件名` 执行。系统 Python 缺少 venv 时先运行安装脚本。机器人自动检测 Chrome、Chromium 或 Edge；检测失败时在 `config.json` 中设置 `browser_executable` 为浏览器可执行文件的绝对路径。

```sh
python deploy.py all --setup-only
python deploy.py all --no-browser
```

独立网站包只需 Flask、Waitress 等少量依赖；机器人包包含 AstrBot 的完整依赖。`requirements.txt` 指向相应清单。不要将运行后的文件夹直接公开；仅提交源码，并排除 `.venv`、`runtime`、`config.json`、AstrBot 的运行数据和凭据。本机原始工作目录保留历史文件及私有数据；本 GitHub 仓库和部署 ZIP 仅包含经过清理的源码与素材。

## 本地验证

安装完成后运行 `.venv/Scripts/python.exe scripts/test_release.py`（Linux 使用 `.venv/bin/python`）。测试覆盖联动开关、独立启动所需模块、查询接口模拟、管理员接口移除及同时写入数据。

## 源码与持续测试

下载或克隆本仓库后，可以直接使用根目录的 Windows / Linux 启动脚本。运行 `python scripts/build_release.py` 会生成六个独立的部署 ZIP，只有清单中明确选定的源码和素材会进入压缩包，运行数据不会被打包。

Linux CI 分别执行独立网站、独立机器人和合并包的系统依赖安装、全新虚拟环境安装、依赖检查、回归测试、HTTP 启动、随机密码登录和实际战绩图片渲染。测试不使用真实的 Domcer 密钥或 QQ 连接；外部战绩 API 的数据路径使用模拟响应验证。Windows 的启动、登录和图片功能已在本机验证。
