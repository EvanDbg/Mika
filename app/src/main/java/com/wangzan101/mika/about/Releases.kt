package com.wangzan101.mika.about

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/** 一个发布的版本:版本号(不带 v)、标题、更新说明(Markdown)、发布日期、发布页、APK 直链。 */
data class ReleaseInfo(
    val version: String,
    val name: String,
    /** 更新说明。 */
    val notes: String,
    /** 发布日期 yyyy-MM-dd;来源没写时为空。 */
    val date: String?,
    val pageUrl: String,
    val apkUrl: String?,
    /** APK 附件的字节数;接口没给时为空。 */
    val apkSize: Long? = null,
    /** APK 附件的摘要,形如 `sha256:<hex>`;较早的附件没有,这时只靠包名、版本号和签名把关。 */
    val apkDigest: String? = null,
) {
    /** 「从 GitHub 下载」打开的地址:有 APK 直链就下 APK,没有就打开发布页。 */
    val downloadUrl: String get() = apkUrl ?: pageUrl
}

/**
 * 读 GitHub 发布页接口(releases/latest)的版本信息:tag_name、name、body、published_at、html_url,
 * 以及 assets 里适合本机的 APK 的 browser_download_url(见 [pickApk])。
 */
object Releases {
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * 发布里除了全架构的 `Mika-v<版本>.apk`,还可能有只带某个架构原生库的包,名字以 `-<后缀>.apk` 结尾。
     * 键是 Android 的 ABI 名,值是附件名后缀。
     */
    private val ABI_APK_SUFFIX = mapOf("arm64-v8a" to "arm64")

    /** [deviceAbis] 是本机支持的 ABI,按偏好排序(Build.SUPPORTED_ABIS);为空时只取全架构包。 */
    fun fromGitHub(text: String, deviceAbis: List<String> = emptyList()): ReleaseInfo =
        fromGitHubObject(json.parseToJsonElement(text).jsonObject, deviceAbis)

    /**
     * Fork 的 main 自动版使用 v<上游版本>-dev.<versionCode> 标签,并标为预发布。
     * 不依赖接口顺序:重新发布的旧版本不能盖过新版;草稿、其他标签和没有可用 APK 的发布都跳过。
     * 没有合适版本时返回空,调用方保留上次查到的更新信息。
     */
    fun fromGitHubDevList(text: String, deviceAbis: List<String> = emptyList()): ReleaseInfo? =
        json.parseToJsonElement(text).jsonArray.asSequence()
            .mapNotNull { it as? JsonObject }
            .filter { o ->
                (o["draft"] as? JsonPrimitive)?.booleanOrNull == false &&
                    (o["prerelease"] as? JsonPrimitive)?.booleanOrNull == true &&
                    DEV_TAG.matches(o.str("tag_name").orEmpty()) &&
                    o.str("tag_name")?.substringAfterLast('.')?.toIntOrNull()?.let { it in 1..2100000000 } == true
            }
            .mapNotNull { o -> runCatching { fromGitHubObject(o, deviceAbis) }.getOrNull() }
            .filter { !it.apkUrl.isNullOrBlank() }
            .maxWithOrNull { a, b -> AppVersion.compare(a.version, b.version) }

    private val DEV_TAG = Regex("""v[0-9]+\.[0-9]+\.[0-9]+-dev\.[1-9][0-9]*""")

    private fun fromGitHubObject(o: JsonObject, deviceAbis: List<String>): ReleaseInfo {
        val tag = o.str("tag_name") ?: error("no tag_name")
        val body = o.str("body").orEmpty()
        val apk = o["assets"]?.let { a ->
            runCatching { a.jsonArray }.getOrNull()?.map { it.jsonObject }?.let { pickApk(it, deviceAbis) }
        }
        val version = AppVersion.normalize(tag)
        return ReleaseInfo(
            version = version,
            name = o.str("name")?.takeIf { it.isNotBlank() } ?: version,
            notes = body.trim(),
            date = o.str("published_at")?.take(10)?.takeIf { it.length == 10 },
            pageUrl = o.str("html_url") ?: "${MikaLinks.RELEASES}/tag/$tag",
            apkUrl = apk?.str("browser_download_url"),
            apkSize = apk?.strOrNumber("size")?.toLongOrNull(),
            apkDigest = apk?.str("digest"),
        )
    }

    /**
     * 本机支持某个架构、发布里又有这个架构的专用包时取专用包(更小);否则取全架构包。
     * 只有别的架构的专用包时不取(装不上),这时打开发布页。
     */
    private fun pickApk(assets: List<JsonObject>, deviceAbis: List<String>): JsonObject? {
        val apks = assets.filter { it.str("name")?.endsWith(".apk", ignoreCase = true) == true }
        fun named(suffix: String) = apks.firstOrNull { it.str("name")!!.endsWith("-$suffix.apk", ignoreCase = true) }
        val forDevice = deviceAbis.firstNotNullOfOrNull { abi -> ABI_APK_SUFFIX[abi]?.let(::named) }
        val universal = apks.firstOrNull { a -> ABI_APK_SUFFIX.values.none { a.str("name")!!.endsWith("-$it.apk", ignoreCase = true) } }
        return forDevice ?: universal
    }

    /** 存盘用:记下最近一次查到的版本,下次打开 App 时红点还在。 */
    fun toJson(r: ReleaseInfo): JsonObject = buildJsonObject {
        put("version", r.version)
        put("name", r.name)
        put("notes", r.notes)
        r.date?.let { put("date", it) }
        put("pageUrl", r.pageUrl)
        r.apkUrl?.let { put("apkUrl", it) }
        r.apkSize?.let { put("apkSize", it) }
        r.apkDigest?.let { put("apkDigest", it) }
    }

    fun fromJson(o: JsonObject): ReleaseInfo? = runCatching {
        ReleaseInfo(
            version = o.str("version")!!, name = o.str("name") ?: o.str("version")!!, notes = o.str("notes").orEmpty(),
            date = o.str("date"), pageUrl = o.str("pageUrl") ?: MikaLinks.RELEASES, apkUrl = o.str("apkUrl"),
            apkSize = o.strOrNumber("apkSize")?.toLongOrNull(), apkDigest = o.str("apkDigest"),
        )
    }.getOrNull()

    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
    private fun JsonObject.strOrNumber(key: String): String? = (this[key] as? JsonPrimitive)?.content
}
