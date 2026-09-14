# handoff-agent

在 opencode 和 qoder CLI 之间切换会话。当 token 额度用完需要换工具时，导出当前会话为统一 markdown，另一个工具读取继续工作。

## 使用方式

### 通过斜杠命令

```bash
# 导出当前会话
/handoff-agent export

# 导入最近的 handoff 文件
/handoff-agent import
```

> **注意：** 斜杠命令会经过 LLM，需要消耗 token。

### 独立命令（不消耗 token）

当 token 额度用完时，退出 agent 后在终端直接运行：

```bash
# 导出
uv run ~/.agents/skills/handoff-agent/scripts/handoff.py export

# 导入（输出文件路径）
uv run ~/.agents/skills/handoff-agent/scripts/handoff.py import
```

也可以设置 shell 别名：

```bash
alias handoff-export='uv run ~/.agents/skills/handoff-agent/scripts/handoff.py export'
alias handoff-import='uv run ~/.agents/skills/handoff-agent/scripts/handoff.py import'
```

## 工作原理

1. 通过环境变量（`QODER_CLI=1`）或数据库查询检测当前工具
2. 通过 pwd 匹配当前项目的会话
3. 导出会话数据（opencode 用 `opencode export`，qoder 读 jsonl）
4. 转换为统一 markdown 格式，保存到 `.handoff/<标题>-latest.md`

## 导出格式

```markdown
# Session Handoff

- **Source Tool**: qoder
- **Session ID**: xxx
- **Session Title**: xxx
- **Directory**: /path/to/project
- **Exported At**: 2026-09-14 15:30:00

## Conversation

### User
用户消息...

### Assistant
助手回复...

#### Tool Call: Bash
```json
{"command": "ls -la"}
```

**Tool Result:**
```
输出内容...
```
```

## 注意事项

- `.handoff/` 建议加入 `.gitignore`
- 同一会话多次导出会覆盖（文件名含 `latest`）
- OpenCode 内置 `/export` 命令可直接导出，不一定需要此 skill
- 脚本使用 Python + uv 内联依赖，跨平台（Windows/Mac/Linux）
