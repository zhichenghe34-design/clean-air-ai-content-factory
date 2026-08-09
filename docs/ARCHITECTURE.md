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

合规审查同时绑定 `review.json` 与 `approved_script.json` SHA-256。人工改稿会重算本地审核和预计朗读时长、撤销旧合规审查，但研究文件未变化时保留研究审查。

每个新任务在创建时固定 `review_policy`，之后不能由浏览器载荷切换身份。普通启动使用 `human/formal`；只有组合启动器显式传入 `-AgentTestReview` 时使用 `agent/test`。代理测试模式的执行者由服务端固定为 `Codex 测试代理`，并写入 `test_only=true`、`human_approval_claimed=false`；它可以推进受控测试，但不构成用户本人签署，成功 manifest 仍等待用户最终成片验收。旧任务缺少该字段时只按历史正式人审口径兼容读取。

## 预算与并发

`BudgetLedger` 在 HTTP 请求发出前增加 `attempted`；成功、HTTP 错误、超时和无效 JSON 都占用同一任务的硬上限 7。研究、研究收束、脚本、修订和模型预审共享台账；连接测试与普通规划不计入任务预算。

同任务同时只有一个执行线程和一个带 PID 的磁盘锁。相同 `Idempotency-Key` 返回已有结果；不同 Key 在运行中返回 409。失效 PID 锁会把最后一个运行标为 `interrupted`。

## 生产适配器

研究层使用受控搜索和 URL 提取。音视频解析配置由字段白名单构造并在子进程结束后删除。配音后检查实际时长；只有 0.75-1.5 倍安全变速能落入 45-60 秒时才继续。

渲染优先使用锁定版 HyperFrames，缺失时返回明确适配器状态；运行时不允许 `npx --yes` 下载。CI 可通过构造参数注入假配音和假渲染适配器，生产默认仍使用真实本地工具。

## Manifest

最终 `manifest.json` 包含 job/run ID、输入哈希、研究/合规审查哈希、结构化 `review_policy`、`evidence_status`、时间、预算统计，以及产物的名称、阶段、MIME、大小和 SHA-256。公开包以 manifest 为准复算文件，不信任报告中的自述状态；代理测试记录不得被包装成正式人审。
