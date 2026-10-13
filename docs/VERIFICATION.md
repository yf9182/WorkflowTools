# WorkflowTools 0.1.0 验证报告

日期：2026-10-13（Asia/Shanghai）。本文区分合成/模拟测试、真实只读集成与尚未验证的发布能力。

## 已完成

| 验证 | 实际结果 | 范围 |
|---|---|---|
| pytest | **59 passed，3.39 秒** | AES-GCM 绑定及篡改、Scrypt 恢复、符号链接/FIFO、仓库外授权边界、文件权限、清理失败 |
| 活跃进程与 SIGKILL | 通过 | 父进程被杀后子进程仍持有继承锁，sweep 不删活跃私钥；子进程退出后可清理 |
| 上传/部署控制 | 通过 | 合成传输：并发上传一次、未知上传不重发、远端回读恢复、Worker 标记恢复、健康失败不重部署、Pages 环境错配拒绝 |
| 数据口径 | 通过 | 退款符号、目标 App 过滤、币种隔离、版本分段、逐日 partition、样本不足、GA4 分页不完整拒绝 |
| 真实 stdio MCP | 通过 | 新/旧协议客户端均连接；10 个工具可发现，结构化返回；与 CLI 同一 Runtime 结果 |
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

## 凭据准备状态

`/Users/f/.config/workflow-tools/` 已创建，0700；连接文件 0600，解密密钥已生成。原 ASC `.p8` 和 Google JSON 已读取并加密导入为 `personal/apple-p8`、`personal/google-service-account`，版本 1。原凭据未修改；日志与工具输出未包含密钥内容。

2026-10-13 复查原项目、授权目录、备份包及本机 Xcode 缓存：匹配团队与 Bundle ID 的 App Store 描述文件已加密导入 `personal/calorie-profile`，版本 1，有效期至 2027-08-11。CMS 签名、分发类型及其包含的证书摘要已核验，与当前钥匙串 Distribution 身份一致。原集中目录的 `CalorieSnapshot_AppStore.mobileprovision` 实际为开发描述文件，未采用。

当前钥匙串中匹配的 Distribution 证书有效期至 2027-08-11；未导出生产私钥，也未改变钥匙串访问权限。未找到可复用 `.p12`。Cloudflare 环境 token 的验证请求返回 401，账户读取返回 403；现有 Wrangler OAuth 缓存已过期，未将这些授权导入套件，也未执行登录或刷新。

仅需补充的材料见 [当前凭据状态](CREDENTIAL_STATUS.md)：R2 桶/只读与写访问配置、有效 Cloudflare 部署 token 与 Account ID、Distribution `.p12`/密码。当前**没有激活云端凭据取用**，不会把显式本地接入验证冒充 R2 集成通过。

## 未验证与保留边界

- 真实 R2 上传/下载、私有桶配置与远端最小权限尚未验证，缺连接访问材料。AES-GCM 与 S3 适配器由合成服务测试覆盖。
- 真实业务 App 的手动签名归档/导出、实际 IPA、TestFlight 上传尚未验证，缺可移植的 `.p12`/密码；匹配主 App 的描述文件已接入。合成证书导入不能替代真实分发验收。
- Cloudflare 业务目标没有实际发布；Workers/Pages 远端发布由合成传输验证，真实只验证 Worker dry-run。
- 新 Mac 端到端迁移尚未实跑；备份/恢复和拒绝覆盖由临时目录测试覆盖。多机并发发布不在首版范围。
- 测试组可用性、Apple Analytics 指标下载、留存/cohort、商店资料、提审/发布和数据库迁移未扩展或未验证。

上传/业务部署由后续具体调用任务触发；不因完成套件实现而生成空版本、邀请测试者或发布站点。遇到未知外部结果保持意图和锁，只回读，不通过清空状态绕过控制。
