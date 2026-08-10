# MoneyPrinterTurbo 第三方边界

本目录记录已验证的第三方视频生产引擎边界，不表示已经把上游源码并入本仓库。

## 固定上游

- 项目：MoneyPrinterTurbo
- 上游仓库：<https://github.com/harry0703/MoneyPrinterTurbo>
- 版本：`1.3.3`
- 固定提交：`254cd028906ee657eab844dc94087cdbea2a7aa8`
- 许可证：MIT，Copyright (c) 2024 Harry
- 许可证 SHA-256：`9065C26334AF5CDA00F023564EED62B6EC297FC2366924018958758BC78A0C26`

固定信息与允许采用范围以 `upstream-lock.json` 为机器可读来源。不得改用浮动分支或浮动版本；任何升级都必须重新审查许可证、依赖、资源和真实输出。

## 责任分界

MoneyPrinterTurbo 只作为候选的独立视频生产引擎，负责经验证后允许接入的素材处理、配音、字幕、任务产物和视频渲染能力。它不得生成或改写已经通过阶段审查的事实脚本，也不得管理 DeepSeek Key、Provider 预算、证据、审查记录或公开任务目录。

时宜 Agent 内容工厂继续负责选题、研究证据、严格反证审核、两道哈希门禁、合规阻断、预算计数、运行隔离、哈希清单和最终发布。受控测试时两道门禁由 Codex 通过浏览器审查，记录明确为代理测试而非用户签署；最终成片再交给用户验收。第三方引擎的输出只有经过本项目重新验证并写入成功 manifest 后，才能成为当前产物。

## 当前采用状态

`source_imported` 当前仍为 `false`，只表示上游 Git 源码没有提交进本仓库。正式便携构建器已经实现从固定 clean snapshot 在独立 staging 中构建 `SHIYI_MPT_OFFLINE_SUBSET_V1`：复制视频生产所需的 Python/JSON 运行依赖闭包，再按机器可验证的固定规则改造成 video-only 子集，并保留本许可证和来源锁。固定脚本 CLI 烟雾、回环 HTTP → `ProductionRunner` 组合联调、便携 Python 导入探针和隔离的真实视频 canary 均已通过；这些是源码/临时 staging 的工程验证，不表示最终对象 ZIP 已重建，也不冒充用户两道人审后的正式成功证据。

便携子集对三处上游文件做确定性适配：`app/router.py` 仅注册视频路由；`app/services/task.py` 移除 LLM、社交发布和外部音乐 Provider 导入，并注入明确拒绝越界调用的离线桩；`app/services/video.py` 将所有正式视频写出（含图片素材分支）与 concat 固定为经验证的 `h264_mf` quality 72 路径，移除 `libx264` 默认值和失败回退。构建器随后实际导入 `app.asgi`，确认 `/api/v1/videos` 与已批准 `video_script`/`video_terms` 路径可用，同时确认 LLM、社交发布和外部音乐 Provider 能力不存在。

随包 MoviePy 的正式身份是 distribution `2.2.1`（上游模块自身仍报告 `2.1.2`，不作为包身份）。构建器在 Python SBOM 生成前应用精确 `shiyi-moviepy-windows-mf` MIT 补丁：只为 `h264_mf` 去掉上游不兼容的 `-preset`，固定 quality 72/`yuv420p`，并同步 `moviepy-2.2.1.dist-info/RECORD`；未知或 writer/RECORD 混合状态直接拒绝。真实 ColorClip + AAC 编码、MPT concat 和 FFprobe 已共同验证输出为 H.264/yuv420p/AAC。适配说明、排除清单、补丁身份和标记由构建器与 verifier 双向校验。

正式 Python 闭包按精确 distribution/RECORD 图从 138 个 distribution 裁剪到 89 个，删除 49 个未采用依赖和重复 ImageIO FFmpeg；最终 5,784 个 RECORD 文件进入逐文件 SBOM。正式 FFmpeg 只能来自仓库锁定的 9 文件 LGPL 共享运行时，不接受任意 FFmpeg 路径或 `libx264` 回退。

允许评估的模块仅限锁文件列出的素材、缓存、任务、产物、配音、字幕、视频服务及非 LLM 视频 API 控制器；为满足这些入口的正常 Python import，可复制其受控运行依赖闭包，但不得借此恢复任何明确排除的能力。实际采用某个模块前仍须检查其依赖、网络行为、输入输出和失败隔离。

## 明确排除

- 不纳入上游 `resource/fonts`、`resource/songs` 和示例媒体；正式字体使用开源可商用的 Noto Sans SC，BGM 默认关闭或使用单独核验过的素材。
- 不纳入上游 WebUI、LLM 生成链路、社交平台上传能力，以及需要外部联网服务的 ElevenLabs/Sonilo 音乐 Provider。
- 不允许 MoneyPrinterTurbo 读取本项目的模型密钥、Cookie、审批数据或运行历史。
- 不声称当前系统具备“智能内容去重”，也不声称已经提供公网 SaaS。

上游 MIT 许可证原文保存在本目录的 `LICENSE`。后续若复制或修改实质性上游代码，发布包必须继续保留该许可证和上游归属说明，并另行记录采用文件与本项目修改内容。
