# Skills

个人 agent skills 合集，支持 opencode、qoder CLI 等 agent 工具。

## 安装

```bash
# 安装单个 skill
npx skills@latest add mmdsnb/skills --skill=obsidian-memory

# 安装所有 skills
npx skills@latest add mmdsnb/skills --skill='*'

# 安装到指定 agent
npx skills@latest add mmdsnb/skills --skill=obsidian-memory -a qoder
```

## Skills 列表

| Skill | 说明 |
|-------|------|
| [obsidian-memory](obsidian-memory/) | AI 的长期记忆：先检索 Obsidian vault 再回答，沉淀后自动 commit/push |
| [agents-md-reflect](agents-md-reflect/) | 从 qoder / opencode 历史会话提炼真实 AGENTS.md，对比现状给差异与证据 |
| [outsource-agent](outsource-agent/) | 把目录/任务外包给便宜模型，干净上下文、只读文件、结果直返 |
| [voter](voter/) | 局域网人工复核：单题或批量逐条投票，图片 + Markdown、进度/跳过/总览，结果 JSON |
| [web-e2e](web-e2e/) | Web 端到端测试规范：Playwright + browserless CDP，tests/ 目录与功能增量约定 |

## License

MIT
