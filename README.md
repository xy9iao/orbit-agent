# Orbit

Orbit 是一个常驻运行的个人 AI 助手：手机上用 Telegram 聊，电脑上用 CLI 聊，共享同一份记忆；
会主动推送提醒和每日简报。没有 agent 框架，全部是可读可改的小模块。

```
Telegram ──┐                                   ┌── 定时任务（晨报 cron）
           ├─▶ Agent loop ─▶ Model Router ─▶ OpenAI-compatible API
CLI ───────┘      │  ▲                          └── 提醒 / Watcher 轮询 ─▶ 推送回 Telegram
                  ▼  │
            tools + memory/*.md + SQLite
```

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env          # 填 OPENAI_API_KEY、TELEGRAM_BOT_TOKEN、TELEGRAM_OWNER_CHAT_ID
.venv\Scripts\orbit serve       # 前台运行守护进程
```

另开一个终端：

```powershell
.venv\Scripts\orbit chat        # 交互对话
.venv\Scripts\orbit send "10 分钟后提醒我喝水"
.venv\Scripts\orbit status      # 运行状态、模型路由、今日用量
.venv\Scripts\orbit reminders   # 待办提醒
.venv\Scripts\orbit briefing    # 立刻生成并推送一份简报
```

Telegram：找 @BotFather 建 bot 拿 token；给 bot 发 `/start`，它会告诉你 chat id，填进 `.env` 重启。
大陆网络在 `.env` 里设 `PROXY_URL`。

## 常驻运行（Windows）

```powershell
.\scripts\install_task.ps1      # 登录即启动，崩溃 1 分钟后自动重启
.\scripts\uninstall_task.ps1
```

日志在 `logs/orbit.log`。电源计划设成"从不睡眠"，否则电脑休眠就离线。

## 配置

- `.env`：密钥和本机相关的值（不提交）。
- `config.yaml`：模型路由。代码里只写任务名（`chat` / `fast` / `summarize` / `briefing`...），
  这里决定每个任务用哪个 provider 的哪个模型。任何 OpenAI-compatible 端点（DeepSeek、Ollama、vLLM）改 `base_url` 即可。
- `persona/system.md`：Orbit 的人设，随时改。`persona/skills/*.md`：按任务加载的额外提示词。
- `memory/MEMORY.md`：Orbit 自己维护的长期记忆（不提交）。

## 加功能

| 想加 | 做法 |
|---|---|
| 新工具 | 在 `orbit/core/tools/` 写一个函数，加 `@tool` 装饰器，在 `tools/__init__.py` import 一下 |
| 新盯梢（Gmail / RSS / 网页） | 继承 `orbit/scheduler/watchers/base.py` 的 `Watcher`，`@register_watcher("xxx")`，在 `config.yaml` 的 `watchers` 启用 |
| 新入口（Discord / Web） | 实现 `orbit/channels/base.py` 的 `Channel` 协议，在 `app.py` 注册 |
| 换模型 | 只改 `config.yaml` |

## 测试

```powershell
.venv\Scripts\pytest
```
