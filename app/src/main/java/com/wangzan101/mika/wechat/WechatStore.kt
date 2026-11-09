package com.wangzan101.mika.wechat

import com.wangzan101.mika.core.run.ChannelPeer
import com.wangzan101.mika.core.wechat.WechatAccount
import com.wangzan101.mika.settings.KeyCipher
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

@Serializable
data class PendingWechatMessage(
    val messageId: String,
    val peer: ChannelPeer,
    val text: String,
    val replyToAskId: String? = null,
)

@Serializable
private data class WechatState(
    val enabled: Boolean = false,
    val cursor: String = "",
    val threads: Map<String, String> = emptyMap(),
    val contextTokens: Map<String, String> = emptyMap(),
    val pending: List<PendingWechatMessage> = emptyList(),
    /** 按确认先后保留最近一段，游标回退或进程恢复时也不会再次登记同一消息。 */
    val acknowledgedMessages: List<String> = emptyList(),
)

@Serializable
private data class WechatUnlockGrant(
    val ownerId: String = "",
    val botId: String = "",
    val allowed: Boolean = false,
)

/** 凭证与运行状态分开保存：令牌加密，游标和未交任务的消息原子落盘。 */
class WechatStore(
    private val dir: File,
    private val cipher: KeyCipher,
    private val unlockFile: File = File(dir, "unlock.json"),
) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val credentials = File(dir, "account.enc")
    private val stateFile = File(dir, "state.json")
    @Volatile private var account: WechatAccount? = readAccount()
    private var state = readState()
    private val _allowUnlock = MutableStateFlow(readUnlockAllowed())
    // The switch reports the durable grant. Block local use immediately while a failed revocation can be retried.
    private var revocationPending = false
    val allowUnlock: StateFlow<Boolean> = _allowUnlock.asStateFlow()

    @Synchronized fun account(): WechatAccount? = account

    @Synchronized fun saveAccount(value: WechatAccount) {
        // 撤销独立授权后才碰凭证与账本；后续写失败、进程重启也不能恢复旧授权。
        setUnlockAllowed(false)
        val sameAccount = account?.let { it.ownerId == value.ownerId && it.botId == value.botId } == true
        if (!sameAccount) {
            // 先清旧账号的游标与账本；同账号重绑则保留原来的消息状态。
            state = WechatState()
            saveState()
        }
        writeAtomic(credentials, cipher.encrypt(json.encodeToString(WechatAccount.serializer(), value).toByteArray()))
        account = value
    }

    @Synchronized fun clear() {
        setUnlockAllowed(false)
        account = null
        state = WechatState()
        credentials.delete()
        saveState()
    }

    @Synchronized fun enabled() = state.enabled
    @Synchronized fun setEnabled(value: Boolean) {
        val next = state.copy(enabled = value)
        if (!value) state = next
        try {
            saveState(next)
        } catch (failure: Exception) {
            // 关闭接收没能落盘时，旧 enabled=true 仍在盘上；撤销独立授权，重启也不能解锁。
            if (!value) try { setUnlockAllowed(false) } catch (revokeFailure: Exception) {
                failure.addSuppressed(revokeFailure)
            }
            throw failure
        }
        state = next
    }

    @Synchronized fun unlockAllowed() = _allowUnlock.value && !revocationPending

    /** 优先原子替换授权记录；无法写入时撤权可通过移除原记录完成。 */
    @Synchronized fun setUnlockAllowed(value: Boolean) {
        if (!value) {
            revocationPending = true
            try {
                writeAtomic(unlockFile, json.encodeToString(WechatUnlockGrant.serializer(), WechatUnlockGrant()).toByteArray())
            } catch (failure: Exception) {
                if (!unlockFile.delete() && unlockFile.exists()) throw failure
            }
            _allowUnlock.value = false
            revocationPending = false
            return
        }
        val current = checkNotNull(account) { "Bind WeChat before allowing unlock" }
        val grant = WechatUnlockGrant(current.ownerId, current.botId, allowed = true)
        writeAtomic(unlockFile, json.encodeToString(WechatUnlockGrant.serializer(), grant).toByteArray())
        revocationPending = false
        _allowUnlock.value = true
    }

    /** 每次实际解锁前重验来源，旧账号、旧线程或其他渠道不能沿用授权。 */
    @Synchronized fun canUnlock(peer: ChannelPeer?): Boolean {
        val current = account ?: return false
        return state.enabled && unlockAllowed() && peer != null &&
            peer.channelId == WechatChannel.ID && peer.peerId == current.ownerId &&
            state.threads[current.ownerId] == peer.threadId
    }

    @Synchronized fun cursor() = state.cursor
    @Synchronized fun saveCursor(value: String) { state = state.copy(cursor = value); saveState() }
    @Synchronized fun contextToken(peerId: String) = state.contextTokens[peerId]

    @Synchronized fun rememberContext(peerId: String, token: String?) {
        if (token.isNullOrBlank() || state.contextTokens[peerId] == token) return
        state = state.copy(contextTokens = state.contextTokens + (peerId to token))
        saveState()
    }

    @Synchronized fun peer(peerId: String): ChannelPeer {
        val thread = state.threads[peerId] ?: "wechat-${UUID.randomUUID()}".also {
            state = state.copy(threads = state.threads + (peerId to it))
            saveState()
        }
        return ChannelPeer(WechatChannel.ID, peerId, thread)
    }

    /** 同一消息只入站一次；未确认的会在下次启动由 [pending] 补交。 */
    @Synchronized fun journal(messageId: String, peer: ChannelPeer, text: String, replyToAskId: String? = null): Boolean {
        if (messageId in state.acknowledgedMessages || state.pending.any { it.messageId == messageId }) return false
        state = state.copy(pending = state.pending + PendingWechatMessage(messageId, peer, text, replyToAskId))
        saveState()
        return true
    }

    @Synchronized fun pending(): List<PendingWechatMessage> = state.pending

    @Synchronized fun acknowledge(messageId: String) {
        if (messageId in state.acknowledgedMessages && state.pending.none { it.messageId == messageId }) return
        state = state.copy(
            pending = state.pending.filterNot { it.messageId == messageId },
            acknowledgedMessages = (state.acknowledgedMessages.filterNot { it == messageId } + messageId)
                .takeLast(MAX_ACKNOWLEDGED_MESSAGES),
        )
        saveState()
    }

    private fun readState(): WechatState = if (stateFile.isFile) runCatching {
        json.decodeFromString(WechatState.serializer(), stateFile.readText())
    }.getOrElse { WechatState() } else WechatState()

    private fun readAccount(): WechatAccount? = if (credentials.isFile) runCatching {
        json.decodeFromString(WechatAccount.serializer(), cipher.decrypt(credentials.readBytes()).decodeToString())
    }.getOrNull() else null

    private fun readUnlockAllowed(): Boolean {
        val current = account ?: return false
        if (!unlockFile.isFile) return false
        return runCatching {
            val grant = json.decodeFromString(WechatUnlockGrant.serializer(), unlockFile.readText())
            grant.allowed && grant.ownerId == current.ownerId && grant.botId == current.botId
        }.getOrDefault(false)
    }

    private fun saveState(value: WechatState = state) =
        writeAtomic(stateFile, json.encodeToString(WechatState.serializer(), value).toByteArray())

    private fun writeAtomic(file: File, bytes: ByteArray) {
        file.parentFile?.mkdirs()
        val temp = File(file.parentFile, file.name + ".tmp")
        FileOutputStream(temp).use { it.write(bytes); it.fd.sync() }
        Files.move(temp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }

    companion object {
        internal const val MAX_ACKNOWLEDGED_MESSAGES = 512
    }
}
