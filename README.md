# Anker AI Notes Exporter

把安克录音豆同步到飞书的录音，导出为自己可以保存和处理的音频、AI Notes 和逐字稿。

这是一个社区维护的本地 Python 工具，适合备份录音、整理知识库或接入自己的工作流。不是安克或飞书的官方项目。

## 功能与边界

- 按日期、最低时长选择最新的一条可见录音。
- 导出原始音频、飞书已有的 AI Notes 和关联逐字稿。
- 根据逐字稿生成本地提取式概要，不调用额外大模型，不向其他 AI 服务上传内容。
- 保存状态清单和音频 SHA-256；重复运行时校验并复用已下载音频。

**前提是录音已同步到飞书，且当前身份有权访问。** 本项目不直接连接录音豆，不提供蓝牙驱动、固件或设备同步服务。

```text
安克录音豆 → 设备配套同步流程 → 飞书录音 / AI Notes
                                      ↓
                         本地 Python + 官方 lark-cli
                                      ↓
                  音频 · AI Notes · 逐字稿 · 本地概要
```

一次运行只处理一个目标，不会常驻轮询或批量备份整个账户。本地概要选取代表性原句，不等于经过确认的会议决策或行动清单。

## 环境

- Python 3.10+；Python 部分只用标准库，无需安装 Python 第三方依赖。
- Node.js / npm，用于安装官方 `lark-cli`。
- 自己的飞书应用 App ID / App Secret，以及该应用下的用户 OAuth 授权。
- 可访问飞书服务的网络环境。

当前链路曾在 macOS、`lark-cli 1.0.97` 下验证，AI Notes 的 `normal` 类型及独立逐字稿文档已有实际导出验证。其他系统、展示类型及未来 CLI 版本需要自行验证。

## 快速开始

### 1. 下载与安装

```bash
git clone https://github.com/chasays/anker-ai-notes-exporter.git
cd anker-ai-notes-exporter
npm install -g @larksuite/cli
```

### 2. 准备应用凭据

在飞书开放平台创建或使用自己的应用。复制示例文件，并填入自己的值：

```bash
cp .env.example .env
# macOS / Linux：限制明文配置文件访问权限
chmod 600 .env
```

格式如下：

```dotenv
APP_ID="YOUR_APP_ID"
APP_SECRET="YOUR_APP_SECRET"
```

脚本默认读取自身目录下的 `.env`，支持注释、单/双引号以及可选的 `export` 前缀，不执行文件或展开变量。含空格、`#` 的值请加引号；这不是完整的 dotenv 插值实现，也不会自动读取 shell 环境变量。

真实 `.env` 是明文，只留在本机，已被 `.gitignore` 排除；仓库只提交占位符模板 `.env.example`。用户 OAuth token 仍由 CLI 管理。

旧 Markdown 和 JSON 配置仍可通过 `--credentials` 显式使用，例如：

```bash
python3 feishu_export.py --profile anker-export --credentials feishu_credentials.md
```

### 3. 配置 CLI 并授权

```bash
lark-cli config init --name anker-export
```

按 CLI 提示配置**与凭据文件相同的应用**，再完成用户授权：

```bash
lark-cli auth login --profile anker-export --domain minutes,note,docs,drive
```

这里的业务域名称是 CLI 授权参数。检查实际请求的权限，只批准所需范围；也可根据 `missing_scopes` 提示用 `--scope` 请求具体权限。应用后台权限、用户授权和资源可见性都需要满足，具体范围以当前 CLI 和飞书返回为准。

App ID / Secret 不能替代用户授权。脚本校验 CLI 会话的 App ID 是否与凭据文件一致；CLI 管理用户 token 和刷新，请勿把 token 写进代码。

### 4. 导出

```bash
# 今天最新的一条可见录音
python3 feishu_export.py --profile anker-export

# 指定日期，选择最新且不少于一小时的录音
python3 feishu_export.py --profile anker-export --date 2026-01-01 --min-duration 3600

# 只导出文档
python3 feishu_export.py --profile anker-export --no-audio
```

日期默认使用本机日期；最低时长单位为秒。

## 已知目标与应用身份

已知 AI Notes 标识或 Docx 链接时，可跳过搜索：

```bash
python3 feishu_export.py --profile anker-export --note-id YOUR_NOTE_ID --no-audio
python3 feishu_export.py --profile anker-export --doc-url YOUR_DOCX_URL
```

`--doc-url` 只导出对应文档。直接指定 `--note-id` 不会推断关联录音以下载音频。

应用身份适合读取应用有权限访问的已知 AI Notes：

```bash
python3 feishu_export.py --identity bot --note-id YOUR_NOTE_ID --no-audio
```

未指定 profile 时，脚本为应用身份建立专用 CLI profile，不修改默认用户 profile。仅凭 App ID / Secret 自动发现最新用户录音尚未在当前配置下跑通；应用身份也不支持 unified Note 逐字稿。完整导出优先使用用户身份。

```bash
python3 feishu_export.py --help
```

帮助中可查看全部参数，包括 `--credentials`、`--output` 和指定录音标识的入口。

## 输出与重复运行

默认输出到脚本旁的 `downloads/日期-标题-标识后六位/`，可用 `--output` 更改：

| 文件 | 内容 |
| --- | --- |
| `audio.*` | 原始音频，后缀按实际媒体类型确定 |
| `ai_notes.md` | 飞书已有的 AI Notes |
| `transcript.md` | 关联逐字稿，成功取得时生成 |
| `summary.md` | 本地提取式概要，有逐字稿时生成 |
| `*.response.json` | 文档原始响应，可能包含附加内容 |
| `manifest.json` | 目标标识、路径、状态、哈希和错误信息 |

重复运行会校验已有音频 SHA-256，匹配时复用；文档、概要和清单会刷新覆盖。自行编辑的内容请另存，或换一个输出目录。

| 退出码 | 含义 |
| --- | --- |
| `0` | 本次执行完成，没有记录导出错误 |
| `1` | 配置、搜索或其他顶层错误 |
| `2` | 部分导出失败；检查清单的 `errors` |

新录音的 AI Notes 或逐字稿可能尚未生成，可稍后重跑。退出码 `0` 不代表所有文件都存在：输出取决于目标类型、资源和参数。

## 常见问题

- **找不到录音**：检查同步状态、日期、最低时长和资源访问权限。用户身份与应用身份的可见资源不同。
- **App ID 不一致**：检查凭据文件与 `--profile` 使用的应用，避免混用不同应用的会话。
- **权限不足**：按 CLI 提示在后台开通权限并完成用户授权。应用身份需要应用权限和资源访问权限。
- **缺少逐字稿或音频**：检查目标入口、AI Notes 类型和生成状态，再看清单错误；完整导出建议走用户搜索入口。
- **代理环境下载失败**：下载保留 HTTPS 证书验证、飞书媒体域名白名单和公网 IP 检查，不跟随媒体重定向；fake-IP 场景会尝试公共 HTTPS DNS。检查网络，不要关闭证书验证。

## 隐私与开源范围

公开版本只包含通用代码、合成测试数据和使用文档。真实录音、AI Notes、逐字稿、客户提案、凭据、CLI 登录态及私人开发历史不在发布内容中。

导出文件和原始响应可能包含姓名、业务内容、文档标识等敏感数据。清单不包含 App Secret 或签名下载 URL，但仍不是匿名数据。分享日志、提交 Issue 或发布导出文件前请先脱敏。如果凭据曾进入公开 Git 历史，需要撤销或轮换；只删除文件不够。

## 开发与验证

```bash
python3 -m unittest -v test_feishu_export.py
python3 -m py_compile feishu_export.py
```

测试覆盖凭据解析、录音选择、下载安全、逐字稿读取和本地文件处理，不需要真实凭据或联网。实际导出仍需设备同步、飞书授权和可访问的录音。

欢迎提交兼容性反馈和改进。请使用虚构或脱敏样例，勿上传真实会议数据。

## 许可证

[MIT](LICENSE)。安克、飞书等名称和商标属于各自权利人。本项目许可证不授予第三方服务、设备固件或会议内容的权利。
