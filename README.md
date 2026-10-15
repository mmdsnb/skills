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
| [handoff-agent](handoff-agent/) | 在 opencode 和 qoder 之间切换会话 |
| [outline-memory](outline-memory/) | 跨 agent 按需读取与沉淀 Outline 长期知识，追加修订保留历史 |

## License

MIT
