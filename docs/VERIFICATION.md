# WorkflowTools 0.1.0 验证报告

日期：2026-10-14（Asia/Shanghai）。本文区分合成/模拟测试、真实只读集成与尚未验证的发布能力。

## 已完成

| 验证 | 实际结果 | 范围 |
|---|---|---|
| pytest | **62 passed，5.81 秒** | 原有安全/安装/部署/分析覆盖，加 ASC provisioning 请求、目标证书匹配、profile 加密登记 |
| 活跃进程与 SIGKILL | 通过 | 父进程被杀后子进程仍持有继承锁，sweep 不删活跃私钥；子进程退出后可清理 |
| 上传/部署控制 | 通过 | 合成传输：并发上传一次、未知上传不重发、远端回读恢复、Worker 标记恢复、健康失败不重部署、Pages 环境错配拒绝 |
| 数据口径 | 通过 | 退款符号、目标 App 过滤、币种隔离、版本分段、逐日 partition、样本不足、GA4 分页不完整拒绝 |
| 真实 stdio MCP | 通过 | 新/旧协议客户端均连接；11 个工具可发现，结构化返回；与 CLI 同一 Runtime 结果 |
| 安装与重复安装 | 通过 | 保留项目配置、其他 MCP、宿主审批策略、用户修改的 Skill |
| 四个 Skill 校验 | 全部有效 | skill-creator quick_validate；无未完成脚手架 |
| Python wheel | 构建及安装通过 | 包含 4 个 Skills、参考文档、Swift 源码、Wrangler 锁文件；无凭据文件 |
| Wrangler 4.149.0 | 真实 dry-run 通过 | Node.js 22；Worker 产出 index.js、map；未上传，关闭自动配置 |
| Swift 签名助手 | 真实编译通过 | macOS Security.framework；密码走 stdin，未传入 argv |
| 合成 PKCS#12 | 真实导入/清理通过 | 独立生成证书和临时钥匙串；串行保护搜索列表，未读取/导出生产签名身份 |
| 临时 Xcode 工程 | 真实解析通过 | xcodebuild 可读取 XML 格式 pbxproj，支持临时副本注入设置 |
| Git 差异检查 | 通过 | CalorieSnapshot 工作树状态与开始时一致，保留用户既有改动 |

## 真实数据读取

在独立临时项目 `/private/tmp/workflow-tools-calorie-readonly`，显式选择本地加密凭据，读取 2026-10-06 至 2026-10-12 的真实数据。原 CalorieSnapshot 未安装新入口，也未改写配置或工程。

- 任务：`8bddf620b1a343f1b3f200c11bb9b1b8`，状态 `succeeded`。
- GA4：真实 API 读取 `ready`，按目标 iOS stream 与版本隔离。
- ASC 销售：真实 API 读取 `ready`，目标 App/订阅过滤。
- Apple Analytics：已有请求/报告目录读取 `ready`；指标行下载仍为 `unverified`。
- JSON 与 Markdown 报告生成成功，无 blocker。原始数据和报告保存在仓库外受限目录，未进入本报告或安装包。

证据目录：

```text
/Users/f/.config/workflow-tools/state/projects/calorie-readonly-verification-f6acf0a04089/artifacts/
  dataset-8bddf620b1a343f1b3f200c11bb9b1b8.json
  report-8bddf620b1a343f1b3f200c11bb9b1b8.json
  report-8bddf620b1a343f1b3f200c11bb9b1b8.md
```

## 凭据与 ASC profile 自动化

个人机器的 `/Users/f/.config/workflow-tools/` 已配置本地凭据与私有 R2。ASC `.p8`、Google JSON、Cloudflare 部署 token、R2 读写凭据、Distribution `.p12`/密码与 CalorieSnapshot profile 已加密导入并按各自能力完成检查；具体版本与验证情况见 [当前凭据状态](CREDENTIAL_STATUS.md)。

本次新增的 `ios_profiles_prepare` 使用现有 ASC Team API `.p8` 查找精确 Distribution 证书，为 Apple 团队内已登记的 Bundle ID 创建/复用 App Store profile，验证后以内存方式加密并登记到项目。通过模拟 ASC 的集成测试覆盖正确证书、Bundle ID、CMS profile 和项目引用；**没有在真实 Apple 团队创建额外描述文件**，所以生产 key 的 Team/Individual 类型及 Provisioning 权限仍须在目标项目首次调用时验证。

## 未验证与保留边界

- 自动创建的描述文件尚未在真实 Apple 团队上运行；若 `.p8` 为 Individual key 或权限不足，工具会返回权限 blocker，不回退到要求手工下载。
- 真实业务 App 的手动签名归档/导出、实际 IPA、TestFlight 上传尚未验证。合成证书导入和 Apple API profile 创建都不能替代真实分发验收。
- Cloudflare 业务目标没有实际发布；Workers/Pages 远端发布由合成传输验证，真实只验证 Worker dry-run。
- 新 Mac 端到端迁移尚未实跑；备份/恢复和拒绝覆盖由临时目录测试覆盖。多机并发发布不在首版范围。
- 测试组可用性、Apple Analytics 指标下载、留存/cohort、商店资料、提审/发布和数据库迁移未扩展或未验证。

上传/业务部署由后续具体调用任务触发；不因完成套件实现而生成空版本、邀请测试者或发布站点。遇到未知外部结果保持意图和锁，只回读，不通过清空状态绕过控制。
