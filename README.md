<p align="center">
  <img src="static/icons/logo.png" width="92" alt="Vibe Session Hub Logo" />
</p>

<h1 align="center">Vibe Session Hub</h1>

<p align="center">
  跨多 AI 编程客户端的 <strong>PR 编号 / Git 分支 / 关键词秒搜 + 一键直跳历史会话</strong> macOS 桌面工具。<br/>
  支持多台 Mac 通过 iCloud Drive 自动无冲突同步，内置 8 款精选深浅配色主题。
</p>

---

## 支持的客户端与原生直跳协议

| 客户端 | 会话采集能力 | 一键直跳方式 |
| :--- | :--- | :--- |
| **Claude Desktop / Claude Magpie** | 标题、Prompt、分支、PR、摘要 | AppleEvent 精准直投进程 `claude://claude.ai/epitaxy/local_...` |
| **Cursor** | 本地 Agent、云端 Background Agent、工作区与分支 | `cursor://anysphere.cursor-deeplink/agent?id=...` 及 `background-agent?bcId=...` |
| **Codex** | 会话上下文、目录、分支、PR | `codex://threads/...` |
| **Gemini 桌面版** | 本地缓存会话与标题 | `googlegemini://conversation/c_...`（未装桌面端自动回退网页） |
| **Grok Bot** | 本地 Agent 会话与分支 | `grokbot://app/v1/agent?id=...` |
| **Antigravity** | Brain / 会话记录与工作区 | 自动唤起对应项目目录并复制标题定位 |
| **网页端会话（ChatGPT / Gemini / Grok）** | 支持手动补充登记 PR 与链接 | 直接打开对应网页会话 URL |

## 快速安装（macOS 13+，零第三方依赖）

本项目仅依赖 macOS 系统自带的 `python3` 与 `swiftc`，克隆后一行命令即可编译原生 `.app` 并配置后台增量索引服务：

```bash
git clone <your-repo-url> vibe-session-hub
cd vibe-session-hub
bash install.sh
```

安装脚本会自动完成：
1. 在父级目录生成原生桌面应用 `Vibe Session Hub.app`（可直接拖入 `/Applications` 或程序坞 Dock）；
2. 执行首次全量增量索引，启动本地服务 `http://127.0.0.1:17380`；
3. 注册 `~/Library/LaunchAgents/com.vibesessionhub.app.plist` 后台守护服务（每 90 秒自动增量采集最新会话）；
4. 若已登录 iCloud Drive，自动按每台 Mac 的硬件 UUID 分片同步至 `~/Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub/nodes/`，多机零写冲突。

## 多台 Mac 一键配对

登录同一 Apple ID 的另一台 Mac 上，可直接从 iCloud Drive 一键安装并配对（也可以直接 `git clone` 后运行 `bash install.sh`）：

```bash
bash "$HOME/Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub/install-on-second-mac.sh"
```

点击界面顶部的设备胶囊标签即可自定义各台电脑的显示名称（如 `MacBook Pro`、`Mac Studio`）。

## 核心快捷键与用法

- **`⌘K`**：聚焦搜索框，输入 **PR 编号**（如 `5590`、`#6306`）、**Git 分支名** 或 **中英文问题关键词**；若会话本身未显式记录 PR 链接，会自动反查本地 Git 提交历史与分支名精准关联。
- **`↑` / `↓` + `↵`**：
  - 选中标记为 **本机** 的会话按回车：直接唤起对应桌面客户端并切入该条历史对话；
  - 选中标记为 **另一台 Mac** 的会话按回车：复制跨机跳转链接（支持 Apple 通用剪贴板直接去另一台电脑打开），或点击 **「复制上下文摘要」** 在当前电脑任意 Agent 中无缝续问。
