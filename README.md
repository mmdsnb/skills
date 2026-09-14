# Skills

个人 agent skills 合集，支持 opencode、qoder CLI 等 agent 工具。

## 安装

```bash
# 安装单个 skill
npx skills@latest add mmdsnb/skills --skill=handoff-agent

# 安装所有 skills
npx skills@latest add mmdsnb/skills --skill='*'

# 安装到指定 agent
npx skills@latest add mmdsnb/skills --skill=handoff-agent -a qoder
```

## Skills 列表

| Skill | 说明 |
|-------|------|
| [handoff-agent](skills/handoff-agent/) | 在 opencode 和 qoder 之间切换会话 |

## License

MIT
