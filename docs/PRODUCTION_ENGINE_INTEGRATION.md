# 纯动画主线与实拍生产引擎边界

## 目标

时宜 Agent 内容工厂保留可信控制层，并把离线纯动画设为 v0.3 默认生产路径：HyperFrames 0.7.86 负责确定性时间线和视频渲染，本地可信动画积木负责可审计的镜头选择；MoneyPrinterTurbo v1.3.3 继续作为已完成工程烟雾验证的实拍素材支线。组合原则是先验证、再采用、最小接入；不是把外部仓库整体混合，也不是把第三方能力冒充为本项目原创。

纯动画模板、注册表和调度代码进入本仓库；HyperFrames 本身仍使用固定 npm 运行时并保留 Apache-2.0 归属，不复制 Vibe Motion、auto-motion 或其他授权不清项目的代码。`third_party/moneyprinterturbo/upstream-lock.json` 中的 `source_imported=false` 表示上游源码没有进入 Git 仓库；它不再表示烟雾验证未完成。固定脚本 CLI 与回环 HTTP → `ProductionRunner` 两条 MPT 工程链均已真实跑通。正式除甲醛候选必须优先走纯动画，再由用户验收最终成片；具体候选证据以项目发布闭环记录为准，本文件不预写动态结果。

| 验证层 | 状态 | 可证明范围 |
|---|---|---|
| 动画注册表与自然中文七幕计划 | 已通过 | 36 个白名单积木、12 个 renderer family、每类 3 个可见变体、行业模式与选择收据哈希绑定 |
| HyperFrames 45 秒 canary strict check | 已通过 | lint/runtime/layout/motion/contrast 全绿，300 个 motion samples；不等于正式成片 E2E |
| HyperFrames 离线依赖许可证闭包 | 已通过 | 135 个可达包；9 个精确版本 override 和 2 个 ONNX NOTICE 引用均有固定来源与哈希 |
| HyperFrames Windows-MF 修改版 | 已通过 | 固定上游/补丁/产物 SHA-256；所有正式 H.264 输出编码路径固定 `h264_mf`，standard quality 72；真实 strict、流式与落盘渲染探针通过 |
| Windows 浏览器信任与启动握手 | 合同与本机真实 canary 已通过；候选冷启动待重跑 | 包内不含 Chrome/CfT/Edge；只接受机器级 `Program Files` 中通过签名、产品身份和四段版本检查的 Edge 151+ |
| LGPL FFmpeg 运行时 | 9 文件与 21 项能力探针已通过 | 自建共享 FFmpeg `d3ad8a7` + zlib 1.3.2；无 `libx264`/GPL/nonfree；`h264_mf` + AAC + yuv420p 及 MPT 所需能力已实测 |
| FFmpeg 对应源码 companion | 本地精确资产已冻结；GitHub Release 上传待完成 | 19,314,160 字节，SHA-256 `A09A2882...56C08`；发布前必须与对象 ZIP 同挂 `v0.3.0` Release |
| 便携 Python 裁剪、SBOM 与 MoviePy 补丁 | 构建器、verifier 和隔离真实 canary 已通过 | 138→89 distributions，5,784 个 RECORD 文件入 SBOM；MoviePy 2.2.1 先补丁 writer/RECORD，再以 `h264_mf` 编码 |
| MPT 固定脚本 CLI | 已通过；manifest `D184B944...B87E5E` | 57.31 秒竖屏 H.264/AAC 与字幕生产能力 |
| MPT HTTP → ProductionRunner | 已通过；manifest `F42EA0D7...DB64F` | 46.600 秒适配器、staging 和媒体复验合同 |
| MPT video-only 便携适配 | 构建器、verifier 和真实 ColorClip→concat→FFprobe canary 已通过 | 仅视频路由与批准脚本/关键词；所有正式视频写出和 concat 固定 `h264_mf` quality 72，无 `libx264` 回退 |
| 纯动画正式任务 + 两道阶段审查 + 用户最终验收 | 每个候选必验 | 代理测试审查必须标记 test_only；用户最终验收另行记录 |
| v0.3 便携包冷启动 | 每个候选必验 | 只有对应 clean commit 的独立记录可提供包哈希与通过结论 |

## 两层职责

### 时宜可信控制层

- 生成三个候选并保留用户执行授权；
- 研究证据、严格反证审核、逐条研究审查与最终脚本审查；
- 合规阻断、七次任务硬预算、Provider 白名单与密钥隔离；
- 不可变运行、失败 staging、审批哈希、manifest 与正式产物发布。

### HyperFrames 纯动画主线

- 新任务默认 `production_mode=motion`；批准脚本被拆成 4—8 幕，并按语义从不可变动画注册表选取本地积木；
- 每幕绑定 block ID、renderer family、行业模式、语义交集与选择收据 SHA-256，不能由模型临时写代码或引用未知动画；
- 只使用有限、同步创建、可 seek 的本地 WAAPI 动画和 Noto Sans SC，不依赖 CDN、随机数、无限循环、远程 runtime 或系统 Node；
- 正式命令固定 HyperFrames `0.7.86`，运行前核对 CLI 所属 `package.json`，版本错配立即阻断；
- Windows 正式运行时必须先对精确 npm bundle 应用 `shiyi-hyperframes-windows-mf` 补丁，再生成运行时清单；H.264 输出固定为 `h264_mf`，补丁身份与 patched CLI SHA-256 写入引擎报告；
- 浏览器不进入便携载荷；启动器只信任机器级 `Program Files` 中签名和产品身份匹配的 Edge 151+，并在报告 motion ready 前执行真实 strict canary；
- 注入式测试 renderer 只能产生 `diagnostic_only` 结果，不能冒充 HyperFrames 或发布正式 manifest。

### MoneyPrinterTurbo 实拍支线

- 只接收已审批脚本、搜索关键词、9:16 与 45–60 秒规格、素材策略、配音策略和独立 staging；
- 只评估锁文件允许的素材、缓存、任务产物、配音、字幕、视频与非 LLM 视频 API 模块；
- 不调用其 LLM，不重新生成脚本，不接触本项目 Key、预算、证据和审批；
- 便携构建只保留 video-only 路由和依赖闭包；MPT 的全部正式视频写出（包括图片素材分支）及 concat 均固定 `h264_mf` quality 72，检测到 `libx264` 默认或回退即拒绝；
- 随包 MoviePy 使用 distribution `2.2.1` 的精确 Windows-MF 补丁，writer 和 wheel `RECORD` 必须同时匹配锁定哈希；
- 失败或超时时只留下当前 staging，不更新上一成功运行。

## 组合门禁

1. 动画主线必须先验证行业模式、积木白名单、逐幕时长、文本上限、相邻去重、收束幕与选择收据，再生成离线工程。
2. HyperFrames `check --strict` 全绿后才允许 `render --no-best-effort --strict`；任何版本、浏览器、字体或依赖闭包不一致均 fail-closed。
3. 实拍支线已在隔离环境以固定脚本和自生成本地素材生成真实 9:16 MP4，没有调用上游 LLM、WebUI 或社交上传。
4. 已用 ffprobe、完整解码和抽帧检查验证 MPT 的 57.31 秒、1080×1920、H.264/AAC、字幕连续性及产物可读性。
5. 已完成回环 HTTP → 控制层 staging 联调并再次验证 46.600 秒成片；测试 `approved` 状态不含人工签名，仅证明适配器合同和媒体复验。
6. 正式便携包才允许从固定 clean snapshot 把所需第三方运行时复制到独立 staging；复制后必须用包内 Node/CLI/FFmpeg 与可信系统 Edge 重新探针并验证逐文件清单，包内不得出现浏览器载荷。
7. 引擎输出只允许导入白名单产物。由时宜控制层重新计算哈希、检查 MIME、媒体规格和视觉门禁，并在全部通过后原子发布。

## 资源与发布边界

- 上游字体、歌曲和示例素材不进入发布包；字体固定使用 Noto Sans SC，BGM 默认关闭或使用已有明确授权和来源记录的素材。
- 上游 WebUI、LLM 和社交平台上传不进入组合版。
- HyperFrames 固定为 `0.7.86` / tag `v0.7.86`，便携包必须保留 Apache-2.0 文本、上游归属、依赖 SBOM 以及每个缺少包内许可证正文的显式审计映射；映射未完成时构建器必须拒绝正式包。
- 正式 FFmpeg 只允许仓库锁定的 9 文件 LGPL-2.1-or-later/Zlib 共享运行时。对应源码 companion 的本地冻结不等于已经公开：发布前必须把精确源码资产与对象代码 ZIP 同时上传同一个 `v0.3.0` GitHub Release，并核验名称、字节数和 SHA-256。
- 正式 Python 运行时必须按精确 distribution/RECORD 图裁剪，移除重复 ImageIO FFmpeg，在最终 MoviePy 补丁之后生成逐文件 SBOM，并同时保留 Python 依赖许可证正文与精确 override。
- 当前不承诺智能内容去重；若只具备 URL、文件哈希或缓存键级去重，必须按其真实能力命名。
- v0.3 的目标交付形态是 Windows 本地一键包，不声明公网 SaaS、多人账号、计费或云端托管能力；发布状态、对应提交和包哈希只由项目发布闭环记录确认，不由本说明预先认定。
- 发布时保留 MoneyPrinterTurbo 的 MIT 许可证、Harry 版权归属、固定版本/提交以及实际采用与修改说明。

## 回退原则

默认优先保持纯动画闭环；实拍引擎不可用时只撤销 `footage` 健康状态，不阻断动画工作台。动画运行时自身未通过固定版本、严格检查或许可证门禁时，不得悄悄降级成伪成功；若到冻结期仍无可靠正式闭环，正式提交继续使用 v0.2.0，v0.3 只作为未发布预览。
