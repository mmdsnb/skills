---
name: voter
description: 对单个命题发起一次人工投票：在局域网网页上展示 Markdown 命题与可选图片，供用户选择「是 / 否 / 其他（从自定义候选中选）」，并返回投票结果 JSON。当需要人工确认图片分类、YOLO/标注复核、二选一或从少量候选中挑一个时使用；服务绑定 0.0.0.0，手机或另一台设备可直接作答。不适用于多题问卷、多人计票或需要登录权限的场景。
---

# 投票器

## 流程

1. `start` 在后台启动服务，把输出 JSON 里的 `url` 发给用户；
2. 用户打开网页投票；
3. `wait` 阻塞等待，输出结果 JSON。投票提交后服务自动退出。

## 启动

短命题：

```sh
uv run --script "<skill_dir>/scripts/voter.py" start \
  --proposition "这张图片是否属于 **cat** 类？" \
  --image ./frame-001.jpg \
  --other cat --other dog \
  --id frame-001
```

多行 Markdown 命题用 stdin：

```sh
uv run --script "<skill_dir>/scripts/voter.py" start --proposition-file - \
  --image ./frame-001.jpg --other cat --other dog <<'EOF'
## 复核
这张图片是否属于 **cat** 类？

- 只判断主体类别
- 看不清按「否」
EOF
```

| 参数 | 说明 |
|---|---|
| `--proposition, -p <文本>` | 命题文本（Markdown） |
| `--proposition-file, -f <路径>` | 从文件读取命题；`-` 表示 stdin |
| `--image, -i <路径>` | 附加图片，可重复 |
| `--other, -o <文本>` | 是/否之外的其他选项，可重复；缺省则只显示是/否 |
| `--id <文本>` | 本次投票标识，写入结果 |
| `--result <路径>` | 会话与结果文件，默认 `./voter-result.json` |
| `--host <地址>` | 监听地址，默认 `0.0.0.0` |
| `--port <端口>` | 默认 `0`（自动分配） |
| `--timeout <秒>` | 服务存活时间，默认 900 |

`start` 输出的 JSON 含 `url`（局域网地址）、`local_url`、`session` 和 `result` 路径。

## 等待结果

```sh
uv run --script "<skill_dir>/scripts/voter.py" wait --timeout 900
```

- 退出码 `0`：已投票，stdout 为结果 JSON。
- 退出码 `124`：超时；服务到时会自动退出并写入 `error: "timeout"`。
- 退出码 `1`：服务异常退出。
- 工具调用本身超时（例如 10 分钟上限）时服务仍在运行，重新执行 `wait` 即可。

结果字段：

```json
{
  "ok": true,
  "pending": false,
  "session": "3f2a9c1b7d4e",
  "id": "frame-001",
  "choice": "cat",
  "choice_type": "other",
  "choice_label": "cat",
  "voted_at": "2026-10-25T19:30:00+08:00"
}
```

- `choice_type`：`yes` / `no` / `other`。
- `choice`：`yes`、`no`，或 `other` 时选中的具体选项。
- `choice_label`：给人看的标签（「是」「否」或选项原文）。

## 规则

- `start` 成功后第一件事是把 `url` 发给用户，然后 `wait`。
- 一次只问一个明确命题；需要多题就在外部循环，每题独立 `start` + `wait`。
- 命题支持标题、粗斜体、代码、列表、引用、表格、链接和 http(s) 图片，不渲染原始 HTML。
- 并发多个投票必须给不同的 `--result`；端口默认随机，不会冲突。
- 页面无鉴权，拿到 URL 的人都能投票；只在本人机器上用时加 `--host 127.0.0.1`。
- 投票完成前不要移动或删除 `--image` 指向的文件。
- 上一次会话的 `.log` 与结果文件可随时删除；排查启动失败时看 `<result>.log`。
