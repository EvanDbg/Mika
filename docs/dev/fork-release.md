# Mika Dev：自动同步上游与发布

这套流程用于 `EvanDbg/Mika` 的长期定制版本：跟进 `wangzan101/Mika` 的 `main`，保留微信任务独立唤醒与解锁授权，通过验证后发布可安装的 Mika Dev APK。提交给上游的功能 PR 分支独立保留。

配置入口为 [fork-sync-release.yml](../../.github/workflows/fork-sync-release.yml)，发布脚本为 [tools/fork/release.py](../../tools/fork/release.py)，构建覆盖配置为 [tools/fork/fork-build.gradle](../../tools/fork/fork-build.gradle)。

## 触发与同步范围

- GitHub Actions 每两小时检查一次，UTC 的偶数小时第 17 分钟运行（`17 */2 * * *`）。GitHub 的调度可能延迟；两次检查之间的上游新提交会合并为一次构建。
- 可在仓库 Actions 的 **Mika Dev upstream sync and release** 中选择 **Run workflow**，分支选 `main`，立即检查。
- 只有 `EvanDbg/Mika` 的 `main` 能执行同步和发布；复制仓库不会自动为其他仓库发布。
- 定时工作流必须保留在默认分支 `main`，并且仓库 Actions 处于启用状态。
- 没有需要发布的新源码时，流程跳过构建。上次构建或发布失败、当前源码尚未成功发布时，下一次检查仍会重试。

工作流在临时 checkout 中将固定的上游提交正常 Git merge 到 fork 源码，再以非强制推送保存到本轮候选分支。正常合并保留 fork 的功能改动与双方提交历史；代码冲突或 GitHub 权限限制会停止本轮，必须处理原因后再运行。流程不强制覆盖分支，也不自动选择一方来消除冲突。

工作流使用同一个并发组串行执行。新的检查不会取消正在构建或发布的版本；后续检查会读取当时的最新上游状态。

## 验证和发布顺序

1. 记录 fork `main`、上游 `main` 与候选合并提交的完整 SHA，生成本轮 `plan.json`。
2. 在候选提交上并行运行四组验证：core JVM 单元测试、Python CLI 契约与测试及翻译检查、Go 网关集成测试、Android 应用单元测试与设备测试编译。
3. 构建未签名的 release APK，包含通用版和 arm64 版。构建任务没有签名密钥。
4. 四组验证全部通过后，在独立任务中读取固定签名，签名并校验 APK 身份、版本、签名与文件摘要。签名任务只使用本轮启动时 `main` 上的控制脚本，不运行候选提交的 Gradle。
5. 发布前再次检查 fork `main` 是否符合本轮预期。只有通过验证的候选提交才推进 `main`；如果期间有人修改了 `main`，停止本轮，下一次重新合并和验证。
6. 创建 draft Release，上传 APK、校验信息和构建信息，验证附件后发布。已发布版本的附件不会被重新覆盖。
7. 清理本轮创建的临时候选分支。

测试或签名失败不会推进 `main`，也不会发布新版本。如果发布阶段在推进 `main` 之后失败，源码可能已同步，但最后成功发布的 APK 仍保留；下一轮根据已发布版本的源码记录重试。不要把工作流运行失败等同于上一个 Release 已失效。

手动恢复时，使用 **Run workflow** 并选择 `main`，或者选择 **Re-run all jobs**，让准备、构建与签名使用同一次尝试生成的计划和附件。**Re-run failed jobs** 不适用于这套发布流程：只重跑失败任务会沿用前一次尝试的计划，提交和尝试编号检查会拒绝它。等待下一次定时检查也会启动完整的新运行。

设备测试在 CI 中只编译，不执行。自动测试和签名验证不代表已完成真机微信收发、亮屏或锁屏解锁验证。

## 版本、下载和应用内更新

Mika Dev 使用独立包名 `com.wangzan101.mika.dev`，可与官方 Mika 并排安装。两个 APK 都是完整安装包：arm64 设备可下载较小的 arm64 版，也可安装通用版；无需同时安装两个文件。

每次新发布分配独立递增的 Android `versionCode`，从 `1000001` 开始，并以此前自动发布的版本序列为基准继续增加；draft 和已经创建的标签也预留对应编号。上游没有修改自己的版本号，也能得到可以覆盖安装的新构建。显示版本和标签保留上游版本及 fork 序号，例如：

```text
上游版本：0.1.38
versionCode：1000001
versionName：0.1.38-dev.1000001
Release tag：v0.1.38-dev.1000001
```

Release 发布在 [EvanDbg/Mika Releases](https://github.com/EvanDbg/Mika/releases)，附带源码提交、上游提交和构建运行信息。自动发布的版本来自上游开发分支；通过 CI 不意味着它是上游作者发布的正式版本。

fork 构建配置将应用内更新源指向 `EvanDbg/Mika`，并保持下载地址、包名、版本与签名检查。后续版本保持相同包名和签名，用户确认安装更新后即可保留应用配置与数据。

### 从最初的测试包迁移

最初的 `wechat-unlock-dev-3e24ca5` APK 使用临时调试签名。这套持续发布使用单独保存的长期签名，因此不能直接覆盖那个临时签名版本。

安装首个长期签名版本前，应先备份或导出可迁移的应用配置，并妥善记录仍需重新配置的项目；然后在手机上处理旧 Mika Dev 与新版本的安装切换。卸载旧 Mika Dev 会删除其应用数据，不能依靠覆盖安装保留。官方 Mika 的独立安装不受这一签名切换影响。

首次切换完成后，后续自动发布始终复用同一签名，支持正常覆盖升级。不要删除或重新生成签名密钥来修复普通构建错误。

## 签名和权限维护

签名保存在 GitHub environment **`mika-dev-release`** 的 Actions secrets 中：

| Secret | 内容 |
| --- | --- |
| `MIKA_DEV_KEYSTORE_BASE64` | 专用签名 keystore 的 Base64 内容 |
| `MIKA_DEV_KEYSTORE_PASSWORD` | keystore 密码 |

该 environment 的部署分支规则限定为 `main`。私钥和密码不得放入源码、日志、Release 或公开 artifact。签名材料只提供给独立签名步骤；发布步骤使用本次工作的 GitHub 令牌，应用构建无需访问签名材料。维护者应安全保存签名备份，避免遗失后无法继续为已安装应用签发更新。

准备、发布及清理任务按需使用 `contents: write`；应用构建与测试使用 `contents: read`。所有 checkout 都关闭凭证持久化。控制脚本和构建来源分别固定到明确提交，不通过推送事件启动下一个工作流：使用 `GITHUB_TOKEN` 的推送不会自动触发 `on: push`，因此所有阶段由本工作流的任务依赖串联。

仓库自带的 `GITHUB_TOKEN` 不具备改写工作流文件的权限。若候选合并会改变 `.github/workflows`，本流程在推送前停止，保留现有 `main` 和 Release，交由维护者检查并正常合并相关工作流调整。这些调整处理完成后，源码自动同步与发布继续运行。其他 GitHub 权限拒绝同样停止本轮；不要通过放宽无关权限或忽略错误来继续发布。

## 常见维护操作

| 情况 | 处理方式 |
| --- | --- |
| 想立即检查上游 | 从 Actions 手动运行 `main` 上的工作流。 |
| 上游与微信改动冲突 | 阅读准备任务的失败信息，在维护分支正常解决合并冲突并验证，再运行自动化。 |
| 单元测试或打包失败 | 查看失败任务及其测试报告，修复后通过 Run workflow（`main`）启动新运行；旧 Release 可继续下载。 |
| 发布中断 | 查看发布任务日志和 draft Release；使用 Run workflow（`main`）或 Re-run all jobs，让整套流程依据源码和版本记录重试，避免手动覆盖已发布 APK。 |
| 同步期间 `main` 被修改 | 使用最新 `main` 再运行；旧候选不会强制覆盖新提交。 |
| 准备任务提示签名配置缺失 | 检查 environment 名称、分支限制和 secrets 配置，保持现有签名不变。 |
| 长时间没有定时运行 | 检查默认分支、Actions 启用状态和 GitHub 的定时工作流停用提示。 |

GitHub 会在公开仓库连续 60 天没有活动时自动停用定时工作流。届时从 Actions 重新启用即可；本流程不通过制造无意义提交来保活。首次配置和变更发布脚本后，建议手动运行一次，确认“同步、验证、签名、发布”完整链路成功。

## 参考

- [GitHub 定时工作流](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub 工作流令牌权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)
- [GITHUB_TOKEN 与工作流触发](https://docs.github.com/en/actions/concepts/security/github_token)
- [Android 应用更新条件](https://developer.android.com/google/play/app-updates)
