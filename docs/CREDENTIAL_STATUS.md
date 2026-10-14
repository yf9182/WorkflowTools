# 本机凭据接入状态

检查日期：2026-10-13。已复用原 CalorieSnapshot 授权；原工程、原授权文件和钥匙串访问控制未修改。凭据本机位于仓库外 `~/.config/workflow-tools/`，导入凭据经 AES-GCM 加密后上传至私有 Cloudflare R2 桶 `workflowtools-vault-2026`，并已逐项远端读回验证；工具不向 Agent 返回明文。

## 已接入，无需再提供

| 材料 | 集中引用 | 核验结果 |
| --- | --- | --- |
| ASC `.p8`、Key ID、Issuer ID、Vendor Number | `personal/apple-p8`，v1 | 真实 ASC 读取通过；身份参数已写入非秘密项目示例 |
| Google 服务账号 JSON、GA4 Property ID | `personal/google-service-account`，v1 | 真实 GA4 读取通过 |
| CalorieSnapshot 主 App 的 App Store 描述文件 | `personal/calorie-profile`，v1 | CMS 签名通过；团队 `Z3YH9HL8H3`、Bundle ID `com.hn.CalorieSnapshot` 匹配；有效期至 2027-08-11；包含现有 Distribution 证书 |
| Cloudflare 部署 API Token | `personal/cloudflare-deploy`，v1 | `WorkflowTools deploy`，一年有效；Workers Editor 与 Pages Write；已验证目标账户部署权限 |
| 私有 R2 只读授权 | `bootstrap/connection.json` | `WorkflowTools vault reader`，永久有效；Object Read only，仅限 `workflowtools-vault-2026`；本地连接文件权限 0600，远端读取验证通过 |
| 私有 R2 上传授权 | `admin/r2-writer.json` | `WorkflowTools vault writer`，永久有效；Object Read & Write，仅限 `workflowtools-vault-2026`；文件权限 0600，已用于加密上传并回读验证 |
| Distribution `.p12` 与密码 | `personal/distribution`、`personal/distribution-password`，v1 | 均已加密上传并远端读回；p12 可解密，证书私钥匹配，团队 `Z3YH9HL8H3`，与主 App Store profile 中的证书指纹匹配 |

App Store 描述文件来自本机 Xcode 缓存。原授权目录里名称为 `CalorieSnapshot_AppStore.mobileprovision` 的文件其实是开发描述文件，未导入。加密导入时的中间明文副本已删除。

钥匙串已存在 `Apple Distribution: Fei Yang (Z3YH9HL8H3)`，证书有效期至 2027-08-11。现有身份已导出为 `.p12`，并由套件验证了私钥、团队、有效期及描述文件证书指纹。不需要另行创建 Distribution 证书。

## 只需补充这些材料

目前没有缺失的本机凭据材料。当前 CalorieSnapshot 工程仅发现主 App Bundle ID `com.hn.CalorieSnapshot`，对应 App Store profile 已就绪。以后安装到含扩展或其他 Bundle ID 的项目时，若该 Bundle ID 已登记且 ASC `.p8` 是有 Provisioning 权限的团队 API key，Agent 可通过 API 自动创建/下载并登记对应 App Store profile；无需用户手工获取文件。

目录 0700，秘密文件 0600。密钥和密码直接写入授权目录，不发到聊天、不写入仓库。详细导入、权限及加密上传命令见 [接入说明](SETUP.md)。

Cloudflare 部署 token 已导入和远端加密托管。当前 CalorieSnapshot 项目尚未安装 WorkflowTools，因此 `doctor --capability ios` 返回 `PROJECT_NOT_INSTALLED`。项目安装后还需填写实际 iOS 工程、scheme，以及若要部署 Cloudflare，则填写目标类型、名称、环境、构建命令和 Wrangler 配置；本次没有执行 TestFlight 上传或 Cloudflare 部署。

所有现有引用均可从 R2 读回。完成目标项目安装与配置后，可执行独立签名构建；具体 TestFlight 上传、真实部署及相关远端结果仍需在目标调用中验证。
