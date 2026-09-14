# Skills

个人 agent skills 合集，支持 opencode、qoder CLI 等 agent 工具。

## 安装

使用 [skills CLI](https://github.com/vercel-labs/skills) 安装：

```bash
# 安装单个 skill
npx skills@latest add mmdsnb/skills --skill=handoff-agent

# 安装所有 skills
npx skills@latest add mmdsnb/skills --skill='*'

# 安装到指定 agent
npx skills@latest add mmdsnb/skills --skill=handoff-agent -a qoder
npx skills@latest add mmdsnb/skills --skill=handoff-agent -a opencode
```

## Skills 列表

### handoff-agent

在 opencode 和 qoder CLI 之间切换会话。当 token 额度用完需要换工具时，导出当前会话为统一 markdown，另一个工具读取继续工作。

**使用方式：**

```bash
# 导出当前会话
/handoff-agent export

# 导入最近的 handoff 文件
/handoff-agent import
```

**独立命令（不消耗 token）：**

```bash
# 直接在终端运行
uv run ~/.agents/skills/handoff-agent/scripts/handoff.py export
uv run ~/.agents/skills/handoff-agent/scripts/handoff.py import
```

> **注意：** OpenCode 内置 `/export` 命令可直接导出，无需此 skill。

## 目录结构

```
skills/
├── handoff-agent/
│   ├── SKILL.md           # Skill 定义
│   └── scripts/
│       └── handoff.py     # 导出/导入脚本
└── README.md
```

## License

MIT
