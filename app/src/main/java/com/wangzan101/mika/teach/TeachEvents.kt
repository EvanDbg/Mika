package com.wangzan101.mika.teach

import android.view.accessibility.AccessibilityEvent
import com.wangzan101.mika.core.teach.TeachEventKind
import com.wangzan101.mika.core.teach.TeachObservation

/** 把一次无障碍事件抄成纯数据。不保留节点，也不在这里写日志。 */
object TeachEvents {
    fun capture(event: AccessibilityEvent, displayId: Int): TeachObservation? {
        val kind = when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_CLICKED -> TeachEventKind.Click
            AccessibilityEvent.TYPE_VIEW_LONG_CLICKED -> TeachEventKind.LongClick
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> TeachEventKind.Text
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> TeachEventKind.Scroll
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> TeachEventKind.Window
            else -> return null
        }
        val source = try {
            event.source
        } catch (e: Exception) {
            null
        }
        return try {
            val password = source?.isPassword == true
            val raw = event.text?.joinToString(" ")?.take(200)?.takeIf { it.isNotBlank() }
            TeachObservation(
                atMs = event.eventTime,
                kind = kind,
                packageName = event.packageName?.toString(),
                className = source?.className?.toString() ?: event.className?.toString(),
                title = if (kind == TeachEventKind.Window) raw else null,
                text = raw,
                description = source?.contentDescription?.toString() ?: event.contentDescription?.toString(),
                viewId = if (password) null else source?.viewIdResourceName,
                password = password,
                displayId = displayId,
            )
        } finally {
            try {
                source?.recycle()
            } catch (e: Exception) {
                // 节点已经回收
            }
        }
    }
}
