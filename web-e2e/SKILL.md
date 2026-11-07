---
name: web-e2e
description: Web 端到端测试（E2E）的项目规范。规定 Playwright Test 如何通过远端 browserless CDP 连接浏览器、测试目录与 spec 如何组织、每个新功能如何追加用例。当需要为新功能补 E2E、回归已有功能，或在项目里运行、调试端到端测试时使用。
---

# Web E2E

## 环境与连接

- 浏览器走远端 browserless CDP，地址由环境变量 `E2E_CDP_ENDPOINT` 提供（含 token）。
- `E2E_CDP_ENDPOINT` 必须带 `timeout` 参数，如 `?token=...&timeout=300000`。browserless 默认 30 秒回收 session，整套用例超过后被远端掐断，表现为 `Target page, context or browser has been closed` 或 `was not bound in the connection`。
- 不要调用 `browser.close()`：CDP 连的是共享浏览器，close 会连带关闭其他 worker 正在用的实例；worker teardown 只断开连接。
- 需要固定视口时，在 `page.goto` 之后调用 `setViewportSize`，导航前设置会被重置为 800x600。
- 被测地址由 `E2E_BASE_URL` 提供，必须是 browserless 侧可达的地址。
- 两个值写在项目根目录 `tests/e2e.env`（gitignore）；`tests/support/env.ts` 手动解析，Playwright 不会自动加载 `.env`。
- 连接方式：worker-scoped fixture 里 `chromium.connectOverCDP(E2E_CDP_ENDPOINT)`。不要用 `use.connectOptions`，它走 Playwright 协议，browserless 只提供 CDP。
- 写用例前先确认可达：`curl '<CDP 的 http 地址>/json/version?token=<token>'`，再用远端浏览器打开 `E2E_BASE_URL`。

## 目录

```
playwright.config.ts
tests/
├── e2e.env
├── support/env.ts
├── support/fixtures.ts    # 导出 test / expect
├── pages/*.page.ts
└── <feature>.spec.ts
```

新项目接入时创建上述文件。config 至少包含：`testDir: './tests'`、`baseURL` 从 env 读、`trace: 'retain-on-failure'`、`screenshot: 'only-on-failure'`、`workers: 4`。

## 用例规范

- `test` / `expect` 只从 `./support/fixtures` 导入
- 一个功能一个 spec，文件名用功能名（kebab-case）
- spec 顶层 `test.describe('<功能名>', { tag: '@<feature>' }, ...)`，场景标题写用户可见行为
- page object 只放定位器和操作，断言留在 spec；`pages/` 在需要时创建
- 历史 spec 只增不删，功能变更时同步更新
- `tests/` 已被单元测试占用时，E2E 放 `tests/e2e/` 并同步改 `testDir`

## 追加新功能

1. 应用已启动的前提下，确认 CDP 和目标地址可达
2. 新建或扩展 `pages/` 下的 page object
3. 新建 `tests/<feature>.spec.ts`
4. 先单跑；合并前全量回归；按标签跑 `npx playwright test --grep @<feature>`

## 隔离与并发

- 用例之间不共享状态；数据自造自清，前置数据走接口
- 并发默认 4，`E2E_WORKERS` 可调，上限由 browserless 决定

## 排查

- CDP 401：`E2E_CDP_ENDPOINT` 里的 token 不对
