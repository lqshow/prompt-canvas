<div align="center">

# Prompt Canvas

### 一个 Codex 本地插件，把 AI 图片迭代从"聊天翻记录"变成"画布管版本"

在画布上管理每一版 AI 生成的图片（v1→v2→v3），通过截图、结构化批注、对话指令三种方式驱动 Codex 生成下一版，所有数据用 SQLite 存在本地。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Codex Plugin](https://img.shields.io/badge/Codex-Plugin-111827)](#安装与启动)
[![MCP Tools](https://img.shields.io/badge/MCP-Tools-2563eb)](./.mcp.json)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933)](./package.json)
[![npm](https://img.shields.io/badge/npm-ready-cb3837)](./package.json)
[![中文](https://img.shields.io/badge/lang-中文-dc2626)](./README.md)
[![English](https://img.shields.io/badge/lang-English-0284c7)](./README.en.md)

**中文** · [English](./README.en.md)

[一分钟上手](#一分钟上手) · [安装与启动](#安装与启动) · [核心能力](#核心能力) · [核心交互](#核心交互) · [工作原理](#工作原理) · [环境变量](#环境变量)

</div>

## 这是什么？

Prompt Canvas 是一个本地优先的 Codex 插件。它让 Codex 打开一个基于 tldraw 的无限画布，帮你生成图片，读取你在图片上的箭头、涂鸦、文字标注，然后把修改后的新版本自动放到旧图右侧。

你可以把它理解成：

**Codex 里的 AI 画图白板。**

普通用户不需要理解 MCP、shape、holder 或本地文件路径。只需要说需求、打开画布、标注修改意见、自动生成新版。

## 一分钟演示

https://github.com/user-attachments/assets/68451222-eee5-48bb-ac75-131f5d8d708d

## 核心能力

| 能力 | 说明 |
|---|---|
| 自然语言生图 | 让 Codex 生成广告图、封面、海报、产品图或视觉概念图 |
| 本地无限画布 | 基于 tldraw 的本地画布，可无限缩放、持续标注 |
| 标注驱动修图 | 箭头、文字、涂鸦会被理解成结构化修改意见 |
| 保留历史版本 | 新版图片放在旧图右侧，旧图保留，方便对比和继续迭代 |
| 本地优先 | 画布数据存在当前项目目录，无需联网后端 |

## 一分钟上手

1. 在 Codex 里说："帮我做一张拉面广告"
2. Codex 打开本地画布，创建 AI Image Holder 并生成图片
3. 在生成的图片上画箭头、写文字、圈出要改的区域
4. 说"按标注修改"，Codex 读取标注并生成新版
5. 新版自动放到右侧，旧图保留，继续迭代

## 安装与启动

### 作为 Codex 插件使用（推荐）

本仓库已按 Codex 标准插件结构配置，支持通过 GitHub 仓库直接分发。

#### 1. 添加 Marketplace（一次性）

在 Codex CLI 中把本仓库添加为 marketplace 来源：

```bash
codex plugin marketplace add lqshow/prompt-canvas
# 或指定分支/SHA
codex plugin marketplace add lqshow/prompt-canvas --ref main
```

添加后，Codex 会从仓库的 `.agents/plugins/marketplace.json` 读取插件列表。

#### 2. 安装并启用插件

1. 打开 Codex 的 **Plugins** 面板。
2. 在 marketplace 选择器里选择 `prompt-canvas-marketplace`。
3. 找到 **Prompt Canvas**，点击 **Install**（或 **Enable**）。
4. 安装完成后，Codex 会自动加载：
   - `skills/prompt-canvas/SKILL.md` 中的 skill 指令
   - `.mcp.json` 中声明的 MCP 服务器

![在 Codex Plugins 面板中找到 Prompt Canvas 并添加](./assets/install-prompt-canvas-plugin.png)

> 如果你不使用插件模式，也可以手动注册 MCP server：
> ```bash
> codex mcp add prompt-canvas -- python3 $(pwd)/mcp-server/prompt_canvas_mcp.py
> ```

#### 3. 安装生图 Skill 依赖

Prompt Canvas 的生图能力依赖我的 `linyuebanzi-image-gen` Skill，需要一起装：

```bash
npx skills add lqshow/linyuebanzi-skills -g
```

#### 4. 配置环境变量（可选）

仓库已提供 `.env.example`，默认端口为 `52846`。如需修改端口，复制一份 `.env`：

```bash
cp .env.example .env
# 编辑 .env 修改 PROMPT_CANVAS_PORT 等变量
```

所有启动脚本（`start-canvas.sh`、`start-mcp.sh`）、Flask 后端、MCP server 和 Vite dev server 都会自动读取 `.env`；未设置 `.env` 时使用 `.env.example` 中的默认值。环境变量优先级最高。

#### 5. 安装依赖并构建前端

```bash
npm install
npm run build
```

#### 6. 启动画布服务

```bash
./scripts/start-canvas.sh /path/to/your/codex-project
```

服务启动后会自动打开：`http://127.0.0.1:52846/`

画布数据会写入项目目录：
- 画布快照：`/path/to/your/codex-project/canvas/pages/<canvas>/prompt-canvas.json`
- 画布资源：`/path/to/your/codex-project/canvas/pages/<canvas>/assets/`
- 待处理提交：`/path/to/your/codex-project/canvas/pages/<canvas>/_pending/`

### 纯本地开发

```bash
# 1. 启动 Flask 后端
python3 server.py

# 2. 开发模式启动前端（Vite dev server 会代理 /api 到 Flask）
npm run dev

# 3. 在浏览器打开
open http://127.0.0.1:5173/canvas/
```

## 核心交互

1. 用户在 Codex 中提出生图需求（如"帮我做一张拉面广告"）
2. Codex 自动创建 AI Image Holder，调用生图 skill 生成图片并填充到画布
3. 用户在图片上画批注（箭头、圈、文字）表达修改意图
4. 用户点击"提交给 Codex"或"复制批注指令"
5. Codex 读取结构化批注 markdown，生成新版本图片
6. 新版本出现在画布上，可继续迭代

![批注模式 + 提交给 Codex](./assets/annotation-submit.png)

提交给 Codex 的批注数据大致长这样：

```json
{
  "canvas_id": "...",
  "target": {
    "shape_id": "ai_xxx",
    "version": "v1",
    "image_url": "/page-assets/xxx/ramen-v1.png",
    "prompt": "..."
  },
  "annotations": [
    {"kind": "draw", "region": "上方", "text": "再加几片牛肉"},
    {"kind": "arrow", "region": "中部", "text": "汤色再浓一点"}
  ],
  "next_version": "v2",
  "md": "...人类可读的 markdown..."
}
```

## 工作原理

- **Skills** (`skills/`)：拆分后的 Codex skill（打开画布、生图、修图/批注）
- **MCP server** (`mcp-server/prompt_canvas_mcp.py`)：把画布能力暴露成 Codex 可调用的工具
- **本地 Web 程序** (`canvas/`)：基于 tldraw 的画布 UI，内嵌在 Codex 浏览器中使用
- **本地服务** (`server.py`)：Flask 后端，持久化到 SQLite (`.cowart.db`)
- **Codex**：理解用户需求、调用 `linyuebanzi-image-gen` 等 skill 生图、回填到画布

## 文件结构

```
.
├── .agents/
│   └── plugins/
│       └── marketplace.json     # Codex marketplace 入口
├── .codex-plugin/
│   └── plugin.json              # Codex 插件元数据
├── .env.example                 # 默认环境变量配置（可提交）
├── .mcp.json                    # MCP 服务器注册
├── package.json                 # npm / Vite 构建配置
├── vite.config.js               # Vite 配置
├── server.py                    # Flask 后端：REST API + SSE + SQLite 持久化
├── imagegen.py                  # 本地 mock 生图（无真实 API 时 fallback 用）
├── canvas/                      # 前端画布源码（tldraw + 自定义 AI Image shape）
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx             # React 入口
│   │   └── styles.css           # 自定义样式
│   ├── dist/                    # Vite 构建产物
│   └── tldraw.css
├── scripts/
│   ├── start-canvas.sh          # 启动画布服务
│   └── start-mcp.sh             # 启动 MCP 服务器
├── mcp-server/
│   └── prompt_canvas_mcp.py     # MCP server，供 Codex 调用
├── skills/
│   └── prompt-canvas/           # Codex skill（打开画布、生图、批注修图）
├── canvas/pages/                # 项目级画布数据（含 assets 与 _pending）
└── .cowart.db                   # SQLite 数据库（运行时生成，保留旧名以兼容历史数据）
```

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `PROMPT_CANVAS_PROJECT_DIR` | 当前 Codex 项目目录 | 当前工作目录 |
| `PROMPT_CANVAS_CANVAS_DIR` | 画布数据存放目录 | `$PROMPT_CANVAS_PROJECT_DIR/canvas` |
| `PROMPT_CANVAS_PORT` | Flask 服务端口 | `52846` |
| `PROMPT_CANVAS_HOST` | Flask 监听地址 | `127.0.0.1` |

## 注意

- `.cowart.db`、`.cowart_state.json`、`canvas/pages/`、`canvas/dist/`、`node_modules/` 等是运行时数据，已被 `.gitignore` 忽略
- 真实生图依赖 Codex 调用外部 skill（如 `linyuebanzi-image-gen`），本地 `imagegen.py` 仅作为 mock fallback
- 生成的图片与 pending submit 文件统一存放在 `canvas/pages/<canvas>/assets/` 与 `canvas/pages/<canvas>/_pending/`，旧的 `/generated/<canvas>/<file>`  URL 仍会重定向到 `/page-assets/<canvas>/<file>` 兼容访问
