package com.wangzan101.mika.about

import com.wangzan101.mika.BuildConfig
import com.wangzan101.mika.R
import com.wangzan101.mika.i18n.tr
import java.net.URLEncoder

/** Mika 对外的地址都在这里;普通构建指向官方,Fork 构建使用自己的发布与反馈入口。 */
object MikaLinks {
    const val REPO_LABEL = "github.com/${BuildConfig.UPDATE_REPOSITORY}"
    const val REPO = "https://$REPO_LABEL"
    const val README = "$REPO#readme"
    const val RELEASES = "$REPO/releases"
    const val NEW_ISSUE = "$REPO/issues/new"
    const val INCLUDE_DEV_RELEASES = BuildConfig.UPDATE_PRERELEASES
    /** latest 不包含预发布;Fork 的 main 自动构建从列表中选自己的 dev 版本。 */
    val LATEST_RELEASE_API = "https://api.github.com/repos/${BuildConfig.UPDATE_REPOSITORY}/releases" +
        if (INCLUDE_DEV_RELEASES) "?per_page=100" else "/latest"

    /** 反馈时预先填进问题模板的设备与版本信息。 */
    data class Device(
        val manufacturer: String,
        val model: String,
        val androidRelease: String,
        val sdk: Int,
        val versionName: String,
        val build: String,
    )

    /** 预先填好的问题正文:机型、安卓版本、Mika 版本三行,下面留空几段给用户写。 */
    fun issueBody(d: Device): String {
        val model = if (d.model.startsWith(d.manufacturer, ignoreCase = true)) d.model else "${d.manufacturer} ${d.model}"
        return buildString {
            append(tr(R.string.issue_body, model.trim(), d.androidRelease, d.sdk, d.versionName, d.build))
        }
    }

    /** 新建问题的地址,标题留空、正文预先填好;参数按 URL 编码(空格写成 %20)。 */
    fun issueUrl(d: Device): String = "$NEW_ISSUE?title=&body=${encode(issueBody(d))}"

    private fun encode(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")
}
