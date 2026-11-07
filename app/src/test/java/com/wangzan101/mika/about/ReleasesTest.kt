package com.wangzan101.mika.about

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReleasesTest {
    private val github = """
        {
          "tag_name": "v0.2.0",
          "name": "Mika 0.2.0",
          "html_url": "https://github.com/wangzan101/Mika/releases/tag/v0.2.0",
          "published_at": "2026-11-05T08:30:00Z",
          "prerelease": false,
          "body": "### 新增\r\n- 附件面板\r\n",
          "assets": [
            {"name": "mika-0.2.0-gpl-sources.tar.gz", "browser_download_url": "https://github.com/x/src.tar.gz"},
            {"name": "mika-0.2.0.apk", "browser_download_url": "https://github.com/x/mika-0.2.0.apk", "size": 12345, "digest": "sha256:abc"}
          ]
        }
    """.trimIndent()

    @Test
    fun readsGitHubRelease() {
        val r = Releases.fromGitHub(github)
        assertEquals("0.2.0", r.version)
        assertEquals("Mika 0.2.0", r.name)
        assertEquals("2026-11-05", r.date)
        assertEquals("https://github.com/wangzan101/Mika/releases/tag/v0.2.0", r.pageUrl)
        assertEquals("https://github.com/x/mika-0.2.0.apk", r.apkUrl)
        assertEquals(r.apkUrl, r.downloadUrl)
        assertEquals(12345L, r.apkSize)
        assertEquals("sha256:abc", r.apkDigest)
        assertTrue(r.notes.contains("附件面板"))
    }

    @Test
    fun gitHubReleaseWithoutApkOrBody() {
        val r = Releases.fromGitHub("""{"tag_name":"0.3.0","name":"","body":null,"html_url":"https://github.com/wangzan101/Mika/releases/tag/0.3.0","assets":[]}""")
        assertEquals("0.3.0", r.version)
        assertEquals("0.3.0", r.name)
        assertEquals("", r.notes)
        assertNull(r.apkUrl)
        assertNull(r.apkSize)
        assertNull(r.apkDigest)
        assertNull(r.date)
        assertEquals(r.pageUrl, r.downloadUrl)
    }

    @Test(expected = Exception::class)
    fun gitHubErrorBodyIsRejected() {
        Releases.fromGitHub("""{"message":"API rate limit exceeded","documentation_url":"https://docs.github.com"}""")
    }

    @Test
    fun storedReleaseRoundTrips() {
        val r = Releases.fromGitHub(github)
        assertEquals(r, Releases.fromJson(Releases.toJson(r)))
        val bare = r.copy(date = null, apkUrl = null)
        assertEquals(bare, Releases.fromJson(Releases.toJson(bare)))
    }

    private val split = """
        {
          "tag_name": "v0.1.40",
          "html_url": "https://github.com/wangzan101/Mika/releases/tag/v0.1.40",
          "assets": [
            {"name": "Mika-v0.1.40-arm64.apk", "browser_download_url": "https://github.com/x/Mika-v0.1.40-arm64.apk", "size": 50, "digest": "sha256:arm"},
            {"name": "Mika-v0.1.40.apk", "browser_download_url": "https://github.com/x/Mika-v0.1.40.apk", "size": 80, "digest": "sha256:all"}
          ]
        }
    """.trimIndent()

    @Test
    fun arm64DevicePicksTheArm64Apk() {
        val r = Releases.fromGitHub(split, listOf("arm64-v8a", "armeabi-v7a", "armeabi"))
        assertEquals("https://github.com/x/Mika-v0.1.40-arm64.apk", r.apkUrl)
        assertEquals(50L, r.apkSize)
        assertEquals("sha256:arm", r.apkDigest)
    }

    @Test
    fun otherDevicesPickTheUniversalApk() {
        for (abis in listOf(listOf("x86_64", "x86"), listOf("armeabi-v7a", "armeabi"), emptyList())) {
            val r = Releases.fromGitHub(split, abis)
            assertEquals(abis.toString(), "https://github.com/x/Mika-v0.1.40.apk", r.apkUrl)
            assertEquals("sha256:all", r.apkDigest)
        }
    }

    @Test
    fun archApkForAnotherDeviceIsNotOffered() {
        val only = """{"tag_name":"v0.1.40","html_url":"https://github.com/wangzan101/Mika/releases/tag/v0.1.40",
            "assets":[{"name":"Mika-v0.1.40-arm64.apk","browser_download_url":"https://github.com/x/Mika-v0.1.40-arm64.apk"}]}"""
        val r = Releases.fromGitHub(only, listOf("x86_64", "x86"))
        assertNull(r.apkUrl)
        assertEquals(r.pageUrl, r.downloadUrl)
        assertEquals("https://github.com/x/Mika-v0.1.40-arm64.apk", Releases.fromGitHub(only, listOf("arm64-v8a")).apkUrl)
    }

    private fun devRelease(
        tag: String,
        draft: Boolean = false,
        prerelease: Boolean = true,
        apk: Boolean = true,
        arm64Only: Boolean = false,
    ): String {
        val asset = if (arm64Only) "Mika-Dev-$tag-arm64.apk" else "Mika-Dev-$tag.apk"
        val assets = if (apk) """[{"name":"$asset","browser_download_url":"https://github.com/EvanDbg/Mika/releases/download/$tag/$asset","size":100}]""" else "[]"
        return """{"tag_name":"$tag","draft":$draft,"prerelease":$prerelease,"assets":$assets}"""
    }

    @Test
    fun devChannelSelectsHighestVersionAndSkipsUnpublishedOrUnrelatedBuilds() {
        val text = listOf(
            devRelease("v0.1.38-dev.1000010"),
            devRelease("v9.0.0-dev.1000100", draft = true),
            devRelease("v9.0.0"),
            devRelease("wechat-unlock-dev-3e24ca5"),
            devRelease("v9.0.0-dev.1000101", prerelease = false),
            devRelease("v9.0.0-dev.1000102", apk = false),
            devRelease("v9.0.0-dev.2147483648"),
            devRelease("v0.1.38-dev.1000009"), // API 的顺序不能变成版本排序。
        ).joinToString(prefix = "[", postfix = "]")
        val found = Releases.fromGitHubDevList(text)!!
        assertEquals("0.1.38-dev.1000010", found.version)
        assertEquals("https://github.com/EvanDbg/Mika/releases/download/v0.1.38-dev.1000010/Mika-Dev-v0.1.38-dev.1000010.apk", found.apkUrl)
    }

    @Test
    fun devChannelRequiresAnApkCompatibleWithTheDevice() {
        val text = "[${devRelease("v0.1.38-dev.1000001")},${devRelease("v0.1.38-dev.1000002", arm64Only = true)}]"
        assertEquals("0.1.38-dev.1000002", Releases.fromGitHubDevList(text, listOf("arm64-v8a"))?.version)
        assertEquals("0.1.38-dev.1000001", Releases.fromGitHubDevList(text, listOf("x86_64"))?.version)
    }

    @Test
    fun malformedDevEntryDoesNotHideAnotherValidRelease() {
        val valid = devRelease("v0.1.38-dev.1000001")
        val text = """[null,{"tag_name":"v9.0.0-dev.1000002","draft":false,"prerelease":true,"assets":[null]},$valid]"""
        assertEquals("0.1.38-dev.1000001", Releases.fromGitHubDevList(text)?.version)
        assertNull(Releases.fromGitHubDevList("[]"))
        assertNull(Releases.fromGitHubDevList("[${devRelease("v0.1.38-dev.1000001", draft = true)}]"))
    }
}
