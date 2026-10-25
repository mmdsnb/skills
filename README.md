# Skills

个人 agent skills 合集，支持 opencode、qoder CLI 等 agent 工具。

## 安装

```bash
# 安装单个 skill
npx skills@latest add mmdsnb/skills --skill=outline-memory

# 安装所有 skills
npx skills@latest add mmdsnb/skills --skill='*'

# 安装到指定 agent
npx skills@latest add mmdsnb/skills --skill=outline-memory -a qoder
```

## Skills 列表

| Skill | 说明 |
|-------|------|
| [outline-memory](outline-memory/) | 跨 agent 按需读取与沉淀 Outline 长期知识，追加修订保留历史 |
| [outsource-agent](outsource-agent/) | 把目录/任务外包给便宜模型，干净上下文、只读文件、结果直返 |
| [voter](voter/) | 局域网人工复核：单题或批量逐条投票，图片 + Markdown、进度/跳过/总览，结果 JSON |

## License

MIT
