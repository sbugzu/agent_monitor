# AI Agent Hardware Monitor for Lilygo T-Encoder Pro

[Lilygo T-Encoder Pro](file:///Users/bug/Workspaces/agent-monitor/temp) 硬件监控系统，实时展示 AI Agent（如 Claude Code, Codex, Aider 及自定义 CLI）的状态。

## 5 色状态与指示灯说明

| 状态 | 颜色 | RGB / Hex | 说明 |
| :--- | :--- | :--- | :--- |
| `IDLE` | **暖白色** | `#FFFDF6` | 任务空闲 / 待命 |
| `THINKING` | **柔和蓝色** | `#A3CCDA` | 正在思考 / 执行 / 调用工具 |
| `COMPLETED_UNREAD` | **柔和绿色** | `#BDE3C3` | 任务完成但尚未查看 |
| `WAITING_APPROVAL` | **柔和黄色** | `#F8F7BA` | 等待批准或用户回复 |
| `ERROR` | **柔和红色** | `#F5D2D2` | 出错 / 任务失败 |

---

## 系统架构与 Hook 抽象层

`agent_monitor` 包含多 Agent 钩子抽象层 (`BaseAgentAdapter`)，允许不同 Agent 通过统一的 Handler 与 API 将状态推送到 Host Daemon，并通过 **USB Serial**、**蓝牙 (BLE UART)** 或 **Wi-Fi** 发送到 Lilygo T-Encoder Pro 硬件。

Host 同时检测本机 Claude、Codex 和 Antigravity 客户端进程：已打开但空闲的
客户端显示为白色 `IDLE`，未启动的客户端不会进入硬件列表；生命周期 hooks
再将在线客户端更新为思考、等待、完成或错误状态。

```
[Claude Code]   [Codex]   [Custom Bot]   [CLI Commands]
      │            │           │               │
      └────────────┴─────┬─────┴───────────────┘
                         ▼
           +---------------------------+
           | BaseAgentAdapter Hub      |  (Host Daemon :8765)
           +-------------┬-------------+
                         ▼
     +---------------------------------------+
     | Hardware Controller (Serial / BLE)    |
     +-------------------┬-------------------+
                         ▼
       +-----------------------------------+
       | Lilygo T-Encoder Pro Hardware     |
       | (5-Color LED Ring + AMOLED UI)    |
       +-----------------------------------+
```

---

## 快速开始

### 1. 运行 Host 服务与模拟器

无需连接硬件即可先通过命令行模拟器体验：

```bash
# 启动 Daemon 守护服务（默认监听 http://127.0.0.1:8765 并在终端渲染硬件模拟器）
python3 -m agent_monitor.main
```

推荐使用启动脚本；需要重新加载代码或配置时可安全重启现有 Host：

```bash
./bin/start_host.sh
./bin/start_host.sh --restart

# 自定义 API 端口时，重启会查找对应端口
./bin/start_host.sh --restart --api-port 9000
```

`--restart` 只会终止监听目标端口且命令行为 `agent_monitor.main` 的进程；
如果端口被其它程序占用，脚本会拒绝终止并退出。

如果开启蓝牙连接模式：
```bash
python3 -m agent_monitor.main --ble
```

### 2. 使用 `agent-hook` 发送 Agent 状态

在另一个终端使用 `bin/agent-hook` CLI 脚本发送状态：

```bash
# 设为思考状态
./bin/agent-hook thinking --agent claude_code -m "Analyzing files..."

# 设为等待批准
./bin/agent-hook wait --agent claude_code -m "Confirm editing app.py?"

# 设为已完成未查看 (绿色)
./bin/agent-hook done --agent claude_code -m "Refactoring finished"

# 确认已读 (绿色 -> 白色)
./bin/agent-hook ack --agent claude_code

# 设为出错 (红色)
./bin/agent-hook error --agent claude_code -m "SyntaxError on line 42"
```

### 3. 包装任意 CLI 命令

可以直接使用 `agent-hook exec` 包装运行任何 CLI 任务：

```bash
./bin/agent-hook exec --agent test_job -- pytest
```

### 4. Waiting 操作菜单

当活动 Agent 处于 `WAITING_APPROVAL` 时，旋钮支持无触摸操作：

- 在监控界面短按进入操作菜单。
- 菜单第一项固定为 `Return`，每次进入都从该项开始。
- 旋转选择操作，短按立即提交；长按约 800ms 返回。
- 旋转后 250ms 内忽略确认，菜单 15 秒无操作会自动返回。
- 请求状态改变、过期或完成提交后，菜单会自动失效。

默认操作为 `Reject`、`Allow Once` 和 `Always Allow`。自定义 Agent 可以在
waiting 事件中传入 `request_id` 和 `actions` 覆盖默认操作；`Return` 仍由
Host 强制置于首位：

```json
{
  "agent": "custom_bot",
  "event": "WAITING_APPROVAL",
  "request_id": "request-42",
  "actions": [
    {"id": "reject", "label": "Reject"},
    {"id": "retry", "label": "Retry"},
    {"id": "always_allow", "label": "Always Allow", "dangerous": true}
  ]
}
```

自定义 Agent 可查询并消费硬件选择：

```text
GET /api/v1/action?request_id=request-42
GET /api/v1/action?request_id=request-42&consume=true
```

Claude Code 的现有 hooks 仍是观察型。Codex 的 `PermissionRequest` hook
会等待硬件选择：`Allow Once` 和 `Reject` 分别返回 Codex 官方的 `allow`
与 `deny` 决策；`Return` 只关闭硬件菜单并保留待确认请求，再次短按可重新
进入。Codex 当前 hook 返回协议不支持持久化 Always Allow，因此硬件上的
Codex 菜单不会提供该选项。Antigravity 的 `run_command` `PreToolUse` hook
同样提供 `Reject` 与 `Allow Once`，选择后分别返回 `deny` 与 `allow`；
超时或硬件离线时返回 `ask`，回退到 Antigravity 原生确认框。

---

## AI Agent (Claude Code / Codex) 钩子配置指南

### Antigravity 原生生命周期钩子

统一安装器会在 Antigravity 的用户级配置中监听 `PreToolUse`、
`PostToolUse`、`PreInvocation`、`PostInvocation` 和 `Stop` 事件，并通过
安装到 `~/.gemini/config/hooks/agent-monitor-hook` 的运行文件转发到 Host
Daemon。`run_command` 进入 `PreToolUse` 时，Host 会显示
`toolCall.args.CommandLine` 中的具体命令并切换到 `WAITING_APPROVAL`；
工具结束后回到执行状态。为避免同一事件发送两次，不要再在工作区
`.agents/hooks.json` 中重复注册 Agent Monitor。安装用户级 hook：

```bash
./bin/install-agent-hooks antigravity
```

Antigravity 要求 `PreToolUse` 返回权限决定；本 hook 最多等待硬件选择
300 秒，`Allow Once` 返回 `allow`，`Reject` 返回 `deny`。当前 hook 协议
没有持久化 Always Allow 的硬件决定，因此不显示该选项；超时或硬件离线时
返回 `ask`，交回 Antigravity 原生确认界面。`Return` 只关闭硬件菜单，
不会提交决定。Antigravity 手动停止时若未发送 `Stop` hook，Host 会在最后
一个 `PostToolUse` 或 `PostInvocation` 事件静默 5 秒后将状态收敛为完成。运行
Antigravity 前请先启动 Host。安装或更新 hooks 后需要重启 Antigravity，
并新建一个任务以重新加载配置：

```bash
./bin/start_host.sh
```

### Claude Code 原生 HTTP Hooks

Claude Code 通过用户级 `~/.claude/settings.json` 将原生生命周期事件直接
POST 到本机 Host：

```text
http://127.0.0.1:8765/api/v1/hooks/claude
```

Host 支持 `UserPromptSubmit`、`PreToolUse`、`PermissionRequest`、
`PostToolUseFailure`、`StopFailure` 和 `Stop`。HTTP hook 使用空的 2xx
响应，不会改变 Claude 的工具调用、权限或停止决策。

Claude 的 API 地址和鉴权信息仅保留在用户级 Claude 配置中；Agent
Monitor 不读取、复制或保存这些凭据。

配置完成后无需 shell alias 或手动调用 `agent-hook`，只需先启动 Host：

```bash
./bin/start_host.sh
```

### 统一 Hook 安装器

安装器支持 `claude`、`codex` 和 `antigravity`。不指定 agent 时处理全部
支持的客户端，也可以按需选择：

```bash
# 查看支持列表
./bin/install-agent-hooks --list

# 安装全部（可安全地重复运行）
./bin/install-agent-hooks

# 只安装 Claude 和 Codex
./bin/install-agent-hooks claude codex

# 检查全部或指定客户端
./bin/install-agent-hooks --check
./bin/install-agent-hooks --check codex

# 卸载指定客户端（保留其它配置和 hooks）
./bin/install-agent-hooks --uninstall antigravity
```

安装器只合并 Agent Monitor 自己的处理器，不覆盖现有客户端配置。
旧命令 `./bin/install-codex-hooks` 继续可用，并转发到统一安装器。

首次安装或 Codex hook 内容变化后，在 Codex 中运行 `/hooks`，检查并信任
新的 command hooks；Codex 会按 hook 定义哈希记录信任，未重新信任的变更
会被跳过。配置变化后请新开 Codex 任务，确保重新加载 hook。Codex
`PermissionRequest` 会将 `tool_input.command`（或工具提供的
`tool_input.description`）显示为具体 waiting 操作，并最多等待硬件选择
300 秒；如果 USB Serial 和 BLE 都已断开，则立即回退到 Codex 原生确认框，
不再等待超时。断开前已经在硬件上选中的决定仍会被正常消费。Host 日志出现
`Agent consumed hardware action` 才表示 Codex hook 已实际取走硬件决定。

---

## 固件烧录 (PlatformIO / Arduino)

固件存放在 `firmware/` 目录：

1. 进入固件目录：`cd firmware`
2. 编译并烧录至 Lilygo T-Encoder Pro:
   ```bash
   pio run -t upload
   ```
