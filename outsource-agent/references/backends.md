# 后端配置参考

## 配置位置

优先级：`--config <路径>` > 环境变量 `OUTSOURCE_AGENT_CONFIG` > `~/.config/outsource-agent/backends.toml`。

配置文件不存在时使用内置后端，不报错。

默认后端优先级：`--backend <名称>` > 环境变量 `OUTSOURCE_AGENT_BACKEND` > 配置 `default_backend` > 内置 `qoder`。

## Schema

```toml
default_backend = "qoder"

[backends.<名称>]
description = "可选说明"
command = ["可执行文件", "参数", "..."]
env = { KEY = "VALUE" }   # 可选，叠加在当前进程环境之上
```

- `command` 必填，字符串数组，不经过 shell。
- prompt 传递：任一参数含 `{prompt}` 时替换为任务全文并从命令行传入；否则写入 stdin。
- 后端必须能自行读取 `--dir` 目录；是否支持、暴露多少权限由它自己的参数决定。

## 内置 qoder

```toml
[backends.qoder]
command = ["qoder", "-p", "-m", "efficient", "--tools", "Glob,Read", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
```

| 参数 | 作用 |
|---|---|
| `-p` | 非交互，打印结果后退出 |
| `-m efficient` | 经济模型（按账号可用模型调整） |
| `--tools "Glob,Read"` | 只给遍历和读取工具，不能写文件或执行命令 |
| `--no-session-persistence` | 不落盘会话 |
| `--setting-sources ""` | 不加载 user/project/local 设置与 AGENTS.md |
| `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` | 不加载任何 MCP 服务器 |

需要更强模型时复制一份改 `-m`，例如 `-m fast`；先用 `qoder --list-models` 确认名称。

## 实测数据

经济模型、目录模式、64px 纯色图：

| 张数 | 耗时 | 轮数 | 上下文占比 | 正确率 |
|---|---|---|---|---|
| 6 | 16.7s | – | – | 6/6 |
| 30 | 24s | – | – | 30/30 |
| 100 | 86s | 8 | 7.5% | 100/100 |

- qoder 有自动上下文压缩（日志中 `auto_compact_model_threshold_caps`，阈值可到 600k）；长批量不会硬中断，但压缩会摘要细节，最终结果仍可能受输出长度限制。
- 真实截图比纯色小图更耗上下文；上百张时先小批试跑，再放大批次。
- 目录外读取需要授权，默认只能读 `--dir` 内文件。
- 100 张约需 90–120 秒，长任务记得调大 `--timeout`。

## 其他 CLI 示例

以下命令以各自 `--help` 为准；第三方后端若开放写文件或执行命令，隔离性自行负责。

```toml
[backends.opencode]
command = ["opencode", "run", "--model", "provider/cheap-model", "{prompt}"]

[backends.codex]
command = ["codex", "exec", "--skip-git-repo-check", "{prompt}"]

[backends.claude]
command = ["claude", "-p", "--model", "haiku"]
```

任何「自行读取当前目录、把答案打到 stdout」的命令都能用作后端。

## 排查

```sh
# 查看生效后端与配置位置
uv run --script scripts/outsource_agent.py backends

# 打印实际命令
uv run --script scripts/outsource_agent.py run --debug --task "ping"
```

失败时先确认：命令是否存在（`command -v <可执行文件>`）、认证是否有效、模型名是否在当前账号可用列表内、后端是否具备读取目录的能力。
