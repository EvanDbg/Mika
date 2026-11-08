package com.wangzan101.mika.chat

import com.wangzan101.mika.cap.sandbox.Attachments
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import java.io.File

/** 系统分享图片的私有副本;[fileName] 只由本 App 生成,不保留外部路径。 */
data class SharedDraftImage(val fileName: String, val name: String, val size: Long)

/** 一个新对话里尚未发送的分享内容。只保存草稿,不包含任何执行状态。 */
data class SharedDraft(val conversationId: String, val text: String, val image: SharedDraftImage? = null)

/**
 * 系统分享草稿按对话保存的落盘点。对话编号由对话页决定;这里只负责让草稿跨进程存下来。
 * 元数据先写临时文件再改名;图片在写入元数据前已完整复制,外部 URI 授权之后失效也能继续编辑和发送。
 */
class SharedDraftStore(private val dir: File) {
    private val metadata: File get() = File(dir, METADATA)

    @Synchronized
    fun read(): SharedDraft? = readAll().lastOrNull()

    @Synchronized
    fun readAll(): List<SharedDraft> = readValid().also { valid ->
        if (valid.isEmpty() && metadata.exists()) metadata.delete()
    }

    /** 保存前要求图片已经是本目录里的完整普通文件;失败时不改旧草稿。 */
    @Synchronized
    fun save(draft: SharedDraft): Boolean {
        if (!valid(draft)) return false
        val old = readValid()
        val next = if (old.any { it.conversationId == draft.conversationId }) {
            old.map { if (it.conversationId == draft.conversationId) draft else it }
        } else old + draft
        return write(next, old)
    }

    /** 发送或删除只清理所属对话,其他会话的分享图片继续可用。 */
    @Synchronized
    fun clear(conversationId: String) {
        val old = readValid()
        if (old.none { it.conversationId == conversationId }) return
        write(old.filterNot { it.conversationId == conversationId }, old)
    }

    @Synchronized
    fun removeImage(conversationId: String) {
        val old = readValid().firstOrNull { it.conversationId == conversationId } ?: return
        if (old.image == null) return
        if (old.text.isBlank()) clear(conversationId) else save(old.copy(image = null))
    }

    /** 尚未绑到对话的副本取消时删除;已经保存成功的同一文件不能删。 */
    @Synchronized
    fun discard(image: SharedDraftImage?) {
        if (image == null || readValid().any { it.image?.fileName == image.fileName }) return
        imageFile(image.fileName)?.delete()
    }

    /** 清掉复制中断的临时文件、没来得及绑到对话的副本和损坏的元数据。 */
    @Synchronized
    fun cleanup() {
        val images = readAll().mapNotNull { it.image?.fileName }.toSet()
        dir.listFiles()?.forEach { f ->
            if (f.name == METADATA || f.name in images) return@forEach
            if (f.isFile) f.delete()
        }
    }

    private fun write(drafts: List<SharedDraft>, old: List<SharedDraft>): Boolean {
        dir.mkdirs()
        if (drafts.isEmpty()) {
            if (metadata.exists() && !metadata.delete()) return false
        } else {
            val json = buildJsonObject {
                put("drafts", JsonArray(drafts.map { draft -> buildJsonObject {
                    put("conversation", draft.conversationId)
                    put("text", draft.text)
                    draft.image?.let { image ->
                        put("image", buildJsonObject {
                            put("file", image.fileName)
                            put("name", image.name)
                            put("size", image.size)
                        })
                    }
                } }))
            }.toString()
            val tmp = File(dir, "$METADATA.tmp")
            val written = runCatching {
                tmp.writeText(json)
                tmp.renameTo(metadata) && metadata.isFile && metadata.readText() == json
            }.getOrDefault(false)
            if (!written) {
                tmp.delete()
                return false
            }
        }
        val retained = drafts.mapNotNull { it.image?.fileName }.toSet()
        old.mapNotNull { it.image?.fileName }.filterNot { it in retained }.forEach { imageFile(it)?.delete() }
        return true
    }

    fun imageFile(fileName: String): File? {
        if (!fileName.matches(FILE)) return null
        val f = File(dir, fileName)
        return f.takeIf { runCatching { it.canonicalFile.parentFile == dir.canonicalFile }.getOrDefault(false) }
    }

    private fun readValid(): List<SharedDraft> = runCatching {
        val root = Json.parseToJsonElement(metadata.readText()) as? JsonObject ?: return emptyList()
        // 兼容原来只有一份分享草稿的文件。
        val entries = (root["drafts"] as? JsonArray)?.toList() ?: listOf(root)
        entries.mapNotNull { entry ->
            val o = entry as? JsonObject ?: return@mapNotNull null
            fun string(obj: JsonObject, key: String) = (obj[key] as? JsonPrimitive)?.contentOrNull
            val image = (o["image"] as? JsonObject)?.let { i ->
                SharedDraftImage(
                    string(i, "file") ?: return@mapNotNull null,
                    string(i, "name") ?: return@mapNotNull null,
                    string(i, "size")?.toLongOrNull() ?: return@mapNotNull null,
                )
            }
            SharedDraft(string(o, "conversation") ?: return@mapNotNull null,
                string(o, "text") ?: return@mapNotNull null, image).takeIf(::valid)
        }
    }.getOrDefault(emptyList())

    private fun valid(draft: SharedDraft): Boolean {
        if (!draft.conversationId.matches(ID) || draft.text.length > MAX_TEXT_CHARS) return false
        if (draft.text.isBlank() && draft.image == null) return false
        val image = draft.image ?: return true
        val file = imageFile(image.fileName)?.takeIf { it.isFile } ?: return false
        return image.name.isNotBlank() && image.name.length <= 240 && image.size >= 0 &&
            image.size <= Attachments.MAX_BYTES && file.length() == image.size
    }

    companion object {
        const val MAX_TEXT_CHARS = 100_000
        private const val METADATA = "draft.json"
        private val ID = Regex("[A-Za-z0-9_-]{1,80}")
        private val FILE = Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
    }
}
