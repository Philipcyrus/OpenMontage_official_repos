<h1 align="center">Panda AI 视频引擎</h1>

<p align="center"><strong>面向 Panda Mobile 的智能体驱动视频生产 —— 输入创意简报，输出带品牌的成片，每一步都有人工审批。</strong></p>

<p align="center">
  <a href="#这是什么">这是什么</a> &nbsp;·&nbsp;
  <a href="#架构">架构</a> &nbsp;·&nbsp;
  <a href="#审批关卡">审批关卡</a> &nbsp;·&nbsp;
  <a href="#快速开始ec2">快速开始</a> &nbsp;·&nbsp;
  <a href="#测试">测试</a> &nbsp;·&nbsp;
  <a href="#仓库结构">仓库结构</a>
</p>

> 🌐 English version: [`README.md`](README.md)。本文件是英文 README 的中文镜像，描述的是 **Panda 生产系统**（而非通用的上游 OpenMontage 引擎）。

---

## 这是什么

一套把**创意简报**变成**Panda Mobile** 成片的生产系统。使用者（通过 **Dify**）提交简报，并在每个阶段审阅产出；**智能体**（Claude Code，驱动 [OpenMontage](https://github.com/calesthio/OpenMontage) 流水线引擎）负责生产 —— 脚本 → 场景规划（文本）→ 定格图 → 运动镜头 + 音频 → 成片。视频/图像生成通过 **Higgsfield MCP** 完成；最终渲染（拼接、中日韩字幕、音频）由已**并入本仓库**的 montage-svc 渲染代码在进程内完成。Panda **品牌元素**（Logo、水印、卡片）**仅在成片通过审批后、按需**叠加 —— 绝不预先烘焙进画面。

所有能力都由**一个 HTTP 服务**（Dify launcher）在 **8501 端口**对外提供。

## 架构

```
Dify (dev.om.mvnoc.ai)                     ← 输入简报，审阅 + 审批
      │  HTTP  (:8501)
Dify Launcher  (dify_launcher/)            ← 唯一的服务；启动/恢复运行，呈现每个关卡
      │  每个任务启动一次
Claude Code + OpenMontage 流水线            ← 智能体：script → scene_plan(文本) → stills → assets → compose
      ├─ Higgsfield MCP        → 视频/图像生成（无需 REST key）
      ├─ ElevenLabs            → 配音 + 配乐
      ├─ panda_render          → 干净合成，ffmpeg 通道（并入的 montage-svc 渲染，进程内）
      └─ video_compose         → remotion / hyperframes 通道（需 Node ≥22；按运行时路由）
      │
本地存储 (data/jobs/{id})                   ← 产物 + 检查点 + cost_report（后续可换 S3，接口不变）

品牌叠加 = 独立的、按需的一步，作用于已通过审批的成片（不是一个关卡）。
第二个入口 /montage/*（独立的 X-Panda-Token）直接暴露底层渲染核心。
```

- **大模型（LLM）：** Claude Code 无头模式 —— 服务器上使用**订阅登录**（`~/.claude`），或通过 `ANTHROPIC_BASE_URL` 走 OpenRouter。无需直连 Anthropic API key。
- **一个服务、一个端口（8501）：** 引擎本身不是服务器；launcher 在其前面承接请求。
- **montage-svc 已并入**（`vendor/montage_svc`）—— 无需再单独运行渲染服务。

## 审批关卡

`panda-video` 流水线最多在**六个**节点暂停以等待人工审批（见 `pipeline_defs/panda-video.yaml`）：

| # | 关卡 | 审阅者审批的内容 |
|---|------|-------------------|
| 1 | `approve_script` | 脚本 |
| 2 | `approve_scene_plan` | 结构化的**文本**场景规划（此阶段尚未生成任何媒体） |
| 3 | `approve_stills` | 每个场景一张定格图 —— **尚未生成视频**，所以此处驳回不产生成本 |
| 3.5 | `approve_motion_sample` | **一段**主镜头样片 —— 在整批生成**之前**先审批运动/动画效果（成本关卡；**默认关闭**，可用 `motion_sample:true` 开启） |
| 4 | `approve_assets` | 完整媒体集（其余定格图动画成镜头 + 配音 + 配乐；可按镜头单独返修） |
| 5 | `approve_final` | 完成的（未叠加品牌的）成片 |

关卡 3、3.5、4 是**同一个** `assets` 阶段的多次暂停（通过 `gate` 字段区分）。`approve_motion_sample` 关卡仅在作业选项 `motion_sample` 开启时出现（**默认关闭**）；需要时设 `motion_sample:true`。批准即推进；“返修（revise）”会重新生成该阶段（或仅指定的镜头）。品牌叠加在关卡 5 **之后**、且仅在被要求时才提供。

## 成本与耗时报告

每个任务都会生成一份按项目汇总的消耗报告，使用各平台**各自的原生单位**（不做跨平台的美元汇总）：**Higgsfield 积分（credits）**、**ElevenLabs** 字符数/秒数，以及分阶段与总计的**生成耗时**。通过 `GET /jobs/{id}/cost`（JSON）获取，或下载 `cost_report.md` 产物。详见 [`dify_launcher/DIFY_INTEGRATION.md`](dify_launcher/DIFY_INTEGRATION.md)。

## 快速开始（EC2）

```bash
git clone https://github.com/Philipcyrus/OpenMontage_official_repos.git ~/panda-engine
cd ~/panda-engine
bash deploy/install.sh          # 系统依赖 + venv + Python 依赖 + 冒烟测试
. .venv/bin/activate
cp .env.example .env            # DIFY_RUNNER=mock、DIFY_DATA_DIR、各类 key；DIFY_TOKEN 可选（留空 = 无鉴权）

# 释放 8501 端口（下线旧的 montage-svc），再启动 launcher
sudo systemctl disable --now montage-svc
uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8501
```

- **鉴权是可选的：** `DIFY_TOKEN` 留空则无需令牌；设置后则要求请求携带 `X-Dify-Token`。
- **Node：** 默认的 `ffmpeg`/`panda_render` 渲染通道无需 Node；`remotion` 与 `hyperframes` 通道需要 **Node ≥ 22**（通过 `nvm` 与系统自带的 Node 18 并存安装）。二者都在 [`deploy/README.md`](deploy/README.md) 中说明。

完整部署（systemd + 反向代理，`dev.om.mvnoc.ai` → 8501）见 [`deploy/README.md`](deploy/README.md)。
把 Dify 接到 launcher 见 [`dify_launcher/DIFY_INTEGRATION.md`](dify_launcher/DIFY_INTEGRATION.md)。

## 运行器（Runners）

设置 `DIFY_RUNNER`：
- **`mock`** —— 不调用 LLM/Higgsfield；伪造脚本与定格图，但会**真实渲染**一段干净视频。用于端到端验证整个 Dify 握手 + 各关卡。
- **`claude`** —— 真实智能体：Claude Code 无头模式 + OpenRouter/订阅登录 + Higgsfield MCP。

## 测试

```bash
python dify_launcher/test_dify_flow.py        # mock 运行器上的完整 5 关卡握手（真实渲染）
python dify_launcher/test_claude_adapter.py   # claude 运行器的检查点适配器
make test                                     # 上游引擎契约测试
```
也可以用 curl 直接驱动线上 API —— 见 [`deploy/README.md`](deploy/README.md)。

## 仓库结构

| 路径 | 说明 |
|------|------|
| `pipeline_defs/panda-video.yaml` | 流水线 + 5 个关卡 |
| `skills/pipelines/panda-video/` | 各阶段技能（场景规划导演、资产导演、合成导演） |
| `lib/cost_report.py` | 按项目的成本与耗时报告（Higgsfield 积分、ElevenLabs 用量、生成耗时） |
| `tools/video/panda_render.py` | 干净合成（并入的渲染） |
| `tools/video/higgsfield_mcp_video.py` | Higgsfield MCP 桥接 |
| `config/panda-elements.json` | 熊猫 + 顾客角色参考 |
| `styles/panda.yaml` | 生成图像的风格 |
| `vendor/montage_svc/`、`vendor/brand/` | 并入的渲染代码 + 品牌资产 + 内置中日韩字体 |
| `dify_launcher/` | Dify 调用的 HTTP 服务（含测试、集成指南） |
| `deploy/` | EC2 安装脚本、systemd 单元、反向代理配置 |
| `lib/`、`schemas/`、`tools/`、`skills/` | OpenMontage 引擎（上游） |

## 致谢与许可

基于 **[OpenMontage](https://github.com/calesthio/OpenMontage)**（智能体化视频流水线引擎）构建。以 **AGPLv3** 许可 —— 见 [`LICENSE`](LICENSE)。
