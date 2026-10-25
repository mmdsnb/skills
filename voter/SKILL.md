---
name: voter
description: 对单个命题或一批逐条复核发起人工投票：在局域网网页上展示 Markdown 命题与图片，点选「是 / 否 / 其他 / 跳过」，返回 JSON 结果。单条用 start；18 项这类批量确认用 batch，在同一服务里逐题展示、显示进度 N/M、支持上一题修改与跳过，最后总览提交。适用于 YOLO/标注复核、分类确认、二选一或从少量候选中挑一个；绑定 0.0.0.0，手机或另一台设备可直接作答。不适用于一屏多题问卷、多人计票或需要登录权限的场景。
---

# 投票器

把「找图 → 回表格 → 判断 → 记进度」压缩成一屏一题：命题、图片、选项、进度同时可见。单题一条 `start`，批量用 `batch`。

脚本只依赖 Python 标准库（3.10+）；可用 `python3` 或 `uv run --script` 运行。

## 选模式

| 场景 | 命令 |
|---|---|
| 确认一条命题 | `start` + `wait` |
| 十几条以上同类复核（如 18 张 YOLO 图） | `batch` + `wait` |
| 多人同时投票、计票 | 不适用 |

## 单题

```sh
python3 "<skill_dir>/scripts/voter.py" start \
  --proposition "这张图片的主体是否属于 **cat** 类？" \
  --image "裁剪框=./frame-001.png" \
  --other cat --other dog --other bird \
  --id frame-001 --progress 1/1
```

多行 Markdown 用 stdin：

```sh
python3 "<skill_dir>/scripts/voter.py" start --proposition-file - \
  --image ./frame-001.png --other cat --other dog <<'EOF'
## 图片分类复核
这张图片的主体是否属于 **cat** 类？

- 只判断画面中央主体
- 看不清按「否」
EOF
```

| 参数 | 说明 |
|---|---|
| `--proposition, -p` | 命题文本（Markdown） |
| `--proposition-file, -f` | 从文件读取；`-` 表示 stdin |
| `--image, -i` | 图片，可重复；`图注=路径` 可加说明 |
| `--other, -o` | 其他选项，可重复；缺省则没有「其他」 |
| `--yes-label / --no-label` | 改写按钮文字，如 `保留 / 删除` |
| `--yes-hint / --no-hint` | 按钮副标题；改了 label 后默认不显示副标题 |
| `--id` | 写出结果时带上标识 |
| `--progress` | 页面角标，如 `7/18` |
| `--result` | 会话与结果文件，默认 `./voter-result.json` |
| `--host` | 默认 `0.0.0.0`；只本机用可给 `127.0.0.1` |
| `--port` | 默认 `0`，随机端口 |
| `--timeout` | 服务存活秒数，默认 900 |

`start` 输出局域网 `url`（带随机 token）、本机 `local_url`、`port`、`session`、`result` 和 `expires_at`。`ok: true, pending: true` 表示服务已启动，不是投票已完成。

启动后先告诉用户端口和两个地址：本机打开 `local_url`，手机或同局域网设备打开 `url`。然后调用 `wait`。

## 批量复核

写一个 manifest（图片路径相对 manifest 文件所在目录）：

```json
{
  "title": "YOLO 标注复核（18 项）",
  "labels": {"yes": "保留", "no": "删除"},
  "other_options": ["cat", "dog", "bird"],
  "items": [
    {
      "id": "778",
      "proposition": "## 778\n图中主体的标注类别是否正确？",
      "images": [
        {"path": "frames/778.png", "caption": "原图 + 当前标注框"}
      ]
    },
    {
      "id": "805",
      "proposition": "## 805\n图中主体的标注类别是否正确？",
      "images": ["frames/805.png"],
      "other_options": ["cat", "dog", "bird", "sheep"]
    }
  ]
}
```

```sh
python3 "<skill_dir>/scripts/voter.py" batch \
  --manifest review.json --timeout 3600 --result review-result.json
```

| manifest 字段 | 说明 |
|---|---|
| `title` | 页面标题 |
| `labels` | 全局按钮文字：`yes`、`no`、`yes_hint`、`no_hint` |
| `other_options` | 全局其他选项；item 可覆盖 |
| `items[].id` | 唯一标识，写入结果；缺省用序号 |
| `items[].proposition` | 必填，Markdown |
| `items[].images` | 路径字符串，或 `{path, caption}`；相对 manifest 目录 |
| `items[].other_options` | 覆盖全局；给 `[]` 表示该题没有「其他」 |

- 一屏一题：命题、图注、选项、进度条同时展示；点图片可开原图。
- 点「是 / 否 / 跳过」立即记录并进下一题；「其他」选完点确认。
- 顶部显示「第 n / N 题 · 已完成」，可回「上一题」修改。
- 全部答完进入总览：逐条核对，点「修改」可回看原题，最后「提交结果」。
- 快捷键：`Y` 是、`N` 否、`O` 其他、`S` 跳过。
- 每答一题都会把进度写进结果文件；服务超时也会保留已答部分（`partial: true`）。

## 等待结果

```sh
python3 "<skill_dir>/scripts/voter.py" wait \
  --result review-result.json --session <start 输出的 session>
```

- `--session` 建议必带：只接受本次会话，避免读到复用结果路径的旧文件；不带时会绑定第一次读到的运行中会话。
- 不传 `--timeout` 时，跟随服务端的 `expires_at` 再等 15 秒；批量请把服务 `--timeout` 设足够长（如 3600）。
- 默认输出精简 JSON（不含命题、图片路径）；需要完整结果加 `--full`，或直接读 `--result` 文件。
- 退出码 `0`：完成；`124`：超时；`1`：服务异常或会话不匹配。
- Agent 的工具调用本身超时（例如 10 分钟上限）时服务仍在跑，重新执行同一条 `wait` 即可。

单题结果：

```json
{
  "ok": true,
  "pending": false,
  "mode": "single",
  "session": "3f2a9c1b7d4e",
  "id": "frame-001",
  "choice": "cat",
  "choice_type": "other",
  "choice_label": "cat",
  "voted_at": "2026-10-25T19:30:00+08:00"
}
```

批量结果：

```json
{
  "ok": true,
  "pending": false,
  "mode": "batch",
  "session": "3f2a9c1b7d4e",
  "summary": {
    "total": 18,
    "answered": 18,
    "counts": {"yes": 12, "no": 4, "other": 1, "skip": 1},
    "other_counts": {"bird": 1}
  },
  "answers": [
    {"index": 0, "id": "778", "choice": "yes", "choice_type": "yes", "choice_label": "保留", "voted_at": "..."}
  ]
}
```

- `choice_type`：`yes` / `no` / `other` / `skip`。
- `choice`：`yes`、`no`、跳过的 `skip`，或 other 时选中的具体选项。
- `choice_label`：页面按钮文字或选项原文。
- `answers` 与 manifest 顺序对齐；超时结果里未答项为 `null`。
- 批量结果不重复存命题，审计时把 manifest 和结果一起保留。

## 规则

- `start` / `batch` 成功后先告诉用户端口和两个地址——本机 `local_url`、局域网 `url`——再调用 `wait`。
- 每条 `wait` 带上 `start` 输出的 `session`；并发投票用不同的 `--result`。
- 同一结果路径已有运行中的会话时，`start` / `batch` 会拒绝启动；等它结束或换路径。
- URL 里有随机 token，拿到完整链接的人就能作答；不要转发到公开渠道。页面无登录，只有别人有链接才能打开。
- 默认绑定 `0.0.0.0`，同一局域网手机可直接打开；只本机用时加 `--host 127.0.0.1`。
- 命题和配置通过管道传给子进程，不出现在 `ps` 命令行里。
- 投票完成前不要移动、改名或删除 manifest 和图片文件；图片按原路径实时读取。
- 结果文件是审计记录，批量时保留；`<result>.log` 只在排查启动失败时有用，可随时删除。
- 一次批量只给一位复核人用；多个标签页同时答题会以后写入的答案为准。
- `skip` 表示「本次不判断」，不是「否」；统计时单独计数。

## 边界

- 不支持一屏多题的表单、多人实时计票、账号权限。
- 需要保存题目快照或跨设备恢复时，保留 manifest + 结果 JSON 即可；服务本身不持久化答案历史，只保留每题最后一次选择。
