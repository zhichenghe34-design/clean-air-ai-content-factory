# v2 架构与数据契约

## 信任边界

控制台仅监听 `127.0.0.1`。浏览器取得随机 HttpOnly 会话 Cookie 和 CSRF token 后，写请求还必须通过 JSON Content-Type 与当前实际端口 Origin 校验。

```text
浏览器控制台
  -> 本地 HTTP API（会话 / CSRF / Origin）
  -> JobStore（状态机、锁、审批、运行目录、manifest）
  -> ProductionRunner（研究 / 内容 / 渲染三个独立阶段）
  -> 登记 Provider 与本地适配器
```

Provider 地址经过规范化。正式模式只允许 DeepSeek 官方 HTTPS 域名、标准端口与根路径或 `/v1`；测试 loopback 必须显式设置 `SHIYI_ALLOW_TEST_PROVIDER=1`。

## Job 与 Run

新任务固定 `schema_version: 2`。任务目录的 `draft/` 只保存当前待审研究、脚本和合规文件。每次 `/run` 创建不可变 `runs/{run_id}/staging/`；成功后原子改名为 `artifacts/`，失败则改名为 `failed/`。

最终成功 render 才更新 `current_run_id`；对已验证成功媒体进行纯报告修正时，可发布独立的 `report_rebuild` 成功运行，预算与媒体哈希不变。普通产物地址只解析当前 run 的 manifest，历史地址必须显式携带成功的 render/report_rebuild `run_id`。旧任务读取时装饰为 `legacy_read_only`，原文件不改写。

## 阶段审查

研究审查绑定 `research.json` SHA-256，并对所有 `auto_review_status=eligible` finding 逐项提交决定与 `verbatim/paraphrase`。自动研究会删除模型输出中的人工审定人、人工时间与 `human_verified` 标签。

合规审查同时绑定 `review.json` 与 `approved_script.json` SHA-256。浏览器改稿会重算本地审核和预计朗读时长、撤销旧合规审查，但研究文件未变化时保留研究审查。编辑身份不信任浏览器自报，而由任务固定的 `review_policy` 在服务端生成结构化 `editor_identity`；代理测试只记录为 `agent/test`，不得写成“人工精修”。

每个新任务在创建时固定 `review_policy`，之后不能由浏览器载荷切换身份。普通源码启动和便携包双击入口使用 `mechanical` 反向机械审核；只有组合启动器显式传入 `-AgentTestReview` 时使用 `agent/test`。代理测试模式的执行者由服务端固定为 `Codex 测试代理`，并写入 `test_only=true`、`human_approval_claimed=false`；它可以推进受控测试，但不构成用户本人签署。机械审核可以无人值守推进阶段，但最终公开发布、品牌、科学和广告合规仍由负责人确认。旧任务缺少该字段时只按历史正式人审口径兼容读取。

## 预算与并发

`BudgetLedger` 在 HTTP 请求发出前增加 `attempted`；成功、HTTP 错误、超时和无效 JSON 都占用同一任务的硬上限 7。研究、研究收束、脚本、修订和模型预审共享台账；连接测试与普通规划不计入任务预算。

同任务同时只有一个执行线程和一个带 PID 的磁盘锁。相同 `Idempotency-Key` 返回已有结果；不同 Key 在运行中返回 409。失效 PID 锁会把最后一个运行标为 `interrupted`。任务创建也使用跨进程 OS 文件锁与持久化 `creation_request`：服务重启后同 Key/同请求仍返回原任务，不同请求固定冲突；服务端核验过的学习规则在首次 `job.json` 原子写入前绑定，不能在创建与规则落盘之间留下半成品任务。

## 生产适配器

研究层使用受控搜索和 URL 提取。音视频解析配置由字段白名单构造并在子进程结束后删除。动画主线固定使用普通中文播报声与 -2% 语速，逐幕生成并实测 PCM；总时长必须在 45-60 秒内、每幕不得超过 4.05 个口播字/秒，不做整轨变速，超限即退回脚本 Agent 重新分镜或改稿。

渲染优先使用锁定版 HyperFrames，缺失时返回明确适配器状态；运行时不允许 `npx --yes` 下载。Windows 便携包内置 Node/HyperFrames 而不携带 Chrome、CfT 或 Edge，启动器仅从机器级 `Program Files` 固定位置解析系统 Edge 151+，并以 Authenticode、Microsoft 产品字段、四段文件版本和真实 strict 浏览器 canary 建立信任；任何宿主覆盖、用户可写路径、缺失或探针失败都不会发布 motion ready。系统浏览器身份和启动 canary 结果会进入健康握手与引擎报告，不能只凭文件存在判定就绪。

正式视频编码输入是仓库逐文件锁定的 9 文件 LGPL 共享 FFmpeg `d3ad8a7` + zlib 1.3.2，不接受任意路径覆盖、旧 BtbN GPL 载荷、第二份 ImageIO FFmpeg 或 `libx264` 回退。HyperFrames 的 Windows 修改版和 MoneyPrinterTurbo video-only 子集都把正式 H.264 输出固定为 `h264_mf`；前者报告补丁/CLI 身份，后者把 MoviePy 写出与 concat 固定为 quality 72。对应 FFmpeg 源码 companion 已在本地冻结，但只有与对象代码 ZIP 同挂 `v0.3.0` GitHub Release 才满足发布门禁。

便携 Python 运行时按精确 distribution、依赖图和 RECORD 所有权从 138 个 distribution 裁剪到 89 个，并在补丁后为 5,784 个 RECORD 文件生成逐文件 SBOM。MoviePy 的发布身份以 distribution `2.2.1` 为准；`shiyi-moviepy-windows-mf` 同时锁定 writer、MIT 修改声明和 wheel `RECORD`，混合或未知状态 fail-closed。CI 可通过构造参数注入假配音和假渲染适配器，生产默认仍使用真实本地工具。

## Manifest

最终 `manifest.json` 包含 job/run ID、输入哈希、研究/合规审查哈希、结构化 `review_policy`、`evidence_status`、时间、预算统计，以及产物的名称、阶段、MIME、大小和 SHA-256。公开包以 manifest 为准复算文件，不信任报告中的自述状态；代理测试记录不得被包装成正式人审。
