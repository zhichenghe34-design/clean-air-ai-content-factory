# MVP 架构与边界

## 产品目标

用户只需要提供目标、素材地址和已有工具目录。控制台负责发现能力、选择本地或 API 后端、生成计划、记录每一步输入输出，并把最终工程交给人工精修。

## 分层

```text
可视化控制台
  ├─ 工作台：目标输入、Agent规划、任务状态
  ├─ 工具发现：扫描本地根目录并分类
  ├─ 接口设置：DeepSeek/OpenAI兼容接口
  └─ 运行记录：job.json + events.jsonl

控制层
  ├─ ConfigStore：非敏感配置与Key生命周期
  ├─ ProjectDiscovery：只读识别本地项目
  ├─ OpenAICompatibleProvider：/models 与 /chat/completions
  └─ Orchestrator：计划、审批、任务状态

能力层（已接入受信生产Adapter，外部下载类Adapter仍待审批）
  ├─ 联网与平台内容提取Skill：HTTP → Playwright → 平台解析 → ASR/OCR
  ├─ 一站式音视频解析
  ├─ 品牌事实库与文本审核
  ├─ Wan/其他视频生成项目或视频API
  ├─ Local Voice Workbench
  ├─ 动态视频导演Skill：脚本→motion_plan.json
  ├─ HyperFrames受信模板：motion_plan→可编辑动画工程
  ├─ FFmpeg/video-autopilot式自动成片
  └─ OpenReel/ChatCut人工精修
```

## 首版执行策略

- 扫描：可直接执行，只读。
- Agent规划：用户主动点击后调用配置的API；没有Key时本地回退。
- 外部工具：只识别、展示和写入计划，默认不执行。
- API Key：默认仅内存保存；用户明确勾选才持久化。
- 任务：每个任务拥有固定目录和事件日志，支持后续恢复。
- 动画：默认走动态导演Skill与HyperFrames；不可用时可降级静态卡片，但运行报告必须标记降级原因。

## 动画生产契约

```text
approved_script.json + review.json + voice.wav
  → motion_plan.json（场景语义、双层运动、连续时间轴）
  → animation_project/（受信HTML模板，不接受模型生成命令或下载地址）
  → HyperFrames check（运行时/布局/运动/对比度）
  → final.mp4
```

项目内Skill位于 `agent-skills/produce-dynamic-health-video/`。模板只接受已登记视觉类型，不让Agent任意注入脚本或从搜索结果安装代码。

## 联网提取契约

```text
用户URL或已登记搜索结果
  → URL与SSRF安全检查
  → 普通HTTP正文提取
  → 正文不足时升级Playwright
  → 抖音/B站/X/YouTube/TikTok升级受信音视频解析Adapter
  → 可选ASR/OCR/关键帧
  → extraction.json + 来源记录 + SHA-256
```

项目内Skill位于 `agent-skills/extract-web-platform-content/`。模型只调用高层 `extract_url` 能力，不接触Shell命令、Cookie文件或任意下载地址。网页文本一律是不可信证据，不能覆盖系统指令或触发工具安装。动态页面失败必须记录并换路；只有登录、验证码、账号权限或所有已安装安全路线均有真实错误时才能停止。

## Adapter 契约（下一阶段）

每个可执行工具必须有显式清单：

```json
{
  "id": "media-analyzer",
  "name": "一站式音视频解析",
  "capabilities": ["content_insight", "asr", "ocr"],
  "entrypoint": "绝对路径",
  "arguments_schema": {},
  "input_contract": {},
  "output_contract": {},
  "healthcheck": {},
  "enabled": false,
  "requires_approval": true
}
```

发现器不能自行生成并执行命令；只有人工审核过的 Adapter 才能进入执行层。
