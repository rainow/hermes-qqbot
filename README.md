# Hermes QQBot Adapter

给 `hermes-agent` 一键安装 QQ Bot，包含：

- `qqbot.py`：QQ Open Platform 适配器实现（网关收消息 + REST 发消息）
- `patch_hermes.py`：自动补丁脚本（把 QQBot 接入到 hermes-agent）
- `deploy.sh`：一键部署脚本（macOS / Linux）
- `deploy.ps1`：一键部署脚本（Windows）

---

## 目录结构

```text
hermes-qqbot/
├── qqbot.py
├── patch_hermes.py
├── deploy.sh
├── deploy.ps1
└── README.md
```

---

## 准备 QQ 机器人

### 1) 申请 QQ 机器人

流程极其简单，只需 3 步：

1. 访问 [QQ 开放平台](https://q.qq.com)
2. 点击页面中的"立即使用"按钮
3. 点击"创建机器人"，填写基本信息后系统会自动分配 `AppID` 和 `AppSecret`

创建成功后，在管理页面复制：
- `AppID` → 对应 `app_id`
- `AppSecret` → 对应 `client_secret`

无需任何繁琐的审核，即可立即使用。

---

## 快速部署

### macOS / Linux

```bash
git clone https://github.com/rainow/hermes-qqbot.git
cd hermes-qqbot
./deploy.sh
```

如果 hermes-agent 不在默认位置，可手动传路径：

```bash
./deploy.sh /path/to/hermes-agent
```

### Windows（PowerShell）

```powershell
# 如果提示执行策略限制，先执行一次：
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

git clone https://github.com/rainow/hermes-qqbot.git
cd hermes-qqbot
.\deploy.ps1
```

如果 hermes-agent 不在默认位置（`~\.hermes\hermes-agent`），可手动传路径：

```powershell
.\deploy.ps1 "C:\path\to\hermes-agent"
```

> 脚本会自动完成：激活 venv → 安装依赖 → 打补丁 → 校验 → 交互式配置凭证。

---

## 配置 QQ 凭证（必填）

运行部署脚本时会交互式询问是否自动写入凭证，也可手动选择以下任意一种方式：

### 方式 A：环境变量

```bash
export QQBOT_APP_ID="你的 app id"
export QQBOT_CLIENT_SECRET="你的 client secret"
```

Windows PowerShell：

```powershell
$env:QQBOT_APP_ID = "你的 app id"
$env:QQBOT_CLIENT_SECRET = "你的 client secret"
```

### 方式 B：`~/.hermes/config.yaml`

```yaml
platforms:
  qqbot:
    enabled: true
    extra:
      app_id: "你的 app id"
      client_secret: "你的 client secret"
      # 也支持：appId / clientSecret（camelCase）
```

---

## 启动 hermes-agent

> **重要**：QQBot 仅在 **gateway 模式**下生效。使用 `hermes` 或者 `hermes chat` 单独开启对话时，QQBot 不会启动。

必须以 gateway 方式启动：

```bash
hermes gateway start
```

gateway 启动后，仍然可以另开一个终端运行 `hermes` 打开 TUI 窗口进行对话。

---

## 当前实现能力

- 支持入站事件：
  - `C2C_MESSAGE_CREATE`
  - `GROUP_AT_MESSAGE_CREATE`
  - `AT_MESSAGE_CREATE`
  - `DIRECT_MESSAGE_CREATE`
- 支持网关连接管理：
  - `HELLO` / `HEARTBEAT` / `READY` / `RESUME` / `RECONNECT` / `INVALID_SESSION`
  - 断线自动重连（指数退避：2s → 5s → 10s → 30s → 60s）
  - 会话恢复（RESUME）避免重复事件
- 支持出站文本发送：
  - C2C / 群聊 / 频道 / 私信
- 支持图片发送：
  - C2C/群聊优先 rich media，失败降级为文本 URL
- 支持输入提示：
  - `send_typing()` 发送 C2C 正在输入状态
- 支持上下文查询：
  - `get_chat_info()` 获取会话信息
  - `get_self_user_id()` 获取机器人自身 ID
- 支持可靠性机制：
  - 入站消息去重（时间窗口 5 分钟）
  - 出站消息 `msg_seq` 去重重试（最多 5 次）
  - 长消息自动分片（每片 ≤2000 字）
  - 附件图片下载缓存

---

## 常见问题

### 1) 日志提示未配置凭证

请确认至少有一组有效凭证来源：

- 环境变量：`QQBOT_APP_ID` + `QQBOT_CLIENT_SECRET`
- 或 `~/.hermes/config.yaml`：`platforms.qqbot.extra.app_id/client_secret`

### 2) 频道消息发送失败

QQ 频道通常要求被动回复 `msg_id`，如果不是从入站消息上下文直接回复，可能被平台拒绝。

### 3) 依赖缺失

部署脚本会自动安装，也可手动安装：

```bash
python -m pip install httpx websockets pyyaml
```

### 4) 长消息分片

超过 2000 字的消息会自动拆分。仅第一条分片携带 `msg_id`（回复引用），后续分片通过 `event_id` + `msg_seq` 去重。

### 5) 消息发送重试

遇到 QQ 平台去重错误（code 40054005）时会自动递增 `msg_seq` 重试，最多 5 次。非去重类错误直接返回失败。

### 6) Windows 下 pip 权限问题

部署脚本使用 `python -m pip` 而非直接调用 `pip`，可绕过大多数权限问题。如果 venv 里缺少 pip，脚本会自动用 `ensurepip` 修复。

---

## 文件说明

### `qqbot.py`

`QQBotAdapter` 主体实现，放入 `hermes-agent/gateway/platforms/qqbot.py` 后生效。

### `patch_hermes.py`

会修改：

- `gateway/config.py`：新增 `Platform.QQBOT`
- `gateway/run.py`：在 `_create_adapter` 中注册 `QQBotAdapter`
- `hermes_cli/gateway.py`：在 `_PLATFORMS` 列表中添加 QQBot，使其出现在 `hermes gateway setup` 向导中
- 并复制 `qqbot.py` 到 `gateway/platforms/`

### `deploy.sh` / `deploy.ps1`

自动执行：

1. 检测并激活 hermes-agent 的 venv
2. 安装 `httpx`、`websockets`、`pyyaml`
3. 调用 `patch_hermes.py`
4. 对补丁结果做基础检查
5. 交互式询问是否配置 QQ 凭证（写入 `~/.hermes/config.yaml`）
