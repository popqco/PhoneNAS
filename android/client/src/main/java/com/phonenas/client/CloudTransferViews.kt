package com.phonenas.client

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

internal class CloudFeedback(val text: String, val error: Boolean = false, val issue: CloudIssue? = null) {
    constructor(issue: CloudIssue) : this(when (issue) {
        CloudIssue.ADDRESS -> "请输入云盘手机的 Tailscale IPv4，或完整的 HTTPS 18889 地址。"
        CloudIssue.LEGACY_HTTP -> "这是旧版 HTTP 云盘地址。此应用需要 PhoneNAS 的 HTTPS 18889 地址，未连接，也未发送登录信息。"
        CloudIssue.IMPORT -> "无法读取公开证书。请重新导出并选择公开证书文件。"
        CloudIssue.FINGERPRINT -> "证书指纹不一致。请用云盘手机上显示的 SHA-256 核对，未发送登录信息。"
        CloudIssue.CERTIFICATE -> "无法验证云盘证书。请检查地址、公开证书和手机日期。"
        CloudIssue.CREDENTIALS -> "请输入有效的账号和密码；用户名不能包含冒号。"
        CloudIssue.VPN -> "未找到通往云盘的 Tailscale 路由。请确认两台手机已连接 Tailscale，再试一次。"
        CloudIssue.UNREACHABLE -> "未能连到此云盘。请确认地址正确，且该地址的 HTTPS 服务正在监听。"
        CloudIssue.AUTH -> "云盘没有接受登录信息。请核对账号和密码。"
        CloudIssue.FORBIDDEN -> "云盘不允许访问这个位置。"
        CloudIssue.NOT_FOUND -> "该文件或文件夹不在这里了。请返回或刷新目录。"
        CloudIssue.RESPONSE -> "这台服务没有返回可识别的 PhoneNAS 文件列表或传输结果。"
        CloudIssue.DOWNLOAD_LENGTH -> "文件已变化或下载中断。请刷新后再下载。"
        CloudIssue.DOWNLOAD_CAPACITY -> "「另存为」最多支持 512 MiB。可选择「下载到手机」。"
        CloudIssue.STAGING -> "手机无法暂存下载。请检查剩余空间。"
        CloudIssue.SAVE -> "没有确认另存完整。请检查所选位置是否留下空文件或部分副本。"
        CloudIssue.DOWNLOAD_SAVE -> "没有保存到下载目录。请检查手机存储空间后重试。"
        CloudIssue.DOWNLOAD_SAVE_UNKNOWN -> "没有确认是否已保存。请先检查「下载 / PhoneNAS」。"
        CloudIssue.PROFILE -> "本机连接未能读取或保存。如旧资料不可读，可在「连接」里移除后重新配对。"
        CloudIssue.CANCELLED -> "已取消。"
        CloudIssue.UPLOAD_SOURCE -> "无法读取所选文件，请重新选择。"
        CloudIssue.UPLOAD_LENGTH -> "读取的文件大小变了。请等文件保存完成后重选。"
        CloudIssue.UPLOAD_CAPACITY -> "文件超过上传限制；大小未知的文件最多暂存 512 MiB。"
        CloudIssue.UPLOAD_STAGING -> "无法准备上传文件。请检查手机剩余空间。"
        CloudIssue.UPLOAD_READ_ONLY -> "这个位置不能写入。请选择一个可读写的普通共享。"
        CloudIssue.UPLOAD_EXISTS -> "同名文件已存在，未覆盖。"
        CloudIssue.UPLOAD_TARGET, CloudIssue.UPLOAD_CONFLICT -> "目标文件名或目录不可用，未确认上传。"
        CloudIssue.UPLOAD_STORAGE -> "云盘存储空间或配额不足。"
        CloudIssue.UPLOAD_BUSY -> "云盘正忙，本次上传未开始。"
        CloudIssue.UPLOAD_UNCONFIRMED -> "云盘没有确认写入。请刷新核对，不要直接重传。"
    }, issue != CloudIssue.CANCELLED, issue)
}

@Composable
internal fun ExchangeDialog(choices: List<DavEntry>, exchange: ExchangeShare?, enabled: Boolean,
                            feedback: CloudFeedback?, onSelect: (DavPath) -> Unit,
                            onRemove: () -> Unit, onDismiss: () -> Unit) {
    var selected by remember(choices) { mutableStateOf<DavPath?>(null) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("选择互传位置") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("选一个专门互传的普通共享。不使用接力镜像、历史或它们的上级目录。")
            if (choices.isEmpty()) Text("未找到可选的顶级共享。")
            else LazyColumn(Modifier.heightIn(max = 178.dp)) {
                items(choices, key = { it.path.encoded }) { entry ->
                    TextButton(onClick = { selected = entry.path }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Text(if (selected == entry.path) "✓ ${entry.path.name}" else entry.path.name)
                    }
                }
            }
            selected?.let { Text("将把「${it.name}」用作独立互传目录。", maxLines = 2, overflow = TextOverflow.Ellipsis) }
            feedback?.let {
                val colors = MaterialTheme.colorScheme
                Surface(color = if (it.error) colors.errorContainer else colors.secondaryContainer,
                    contentColor = if (it.error) colors.onErrorContainer else colors.onSecondaryContainer,
                    shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth()) {
                    Text(it.text, Modifier.padding(10.dp), style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }, confirmButton = {
        Button(onClick = { selected?.let(onSelect) }, enabled = enabled && selected != null) { Text("设为互传共享") }
    }, dismissButton = {
        if (exchange != null && selected == null) TextButton(onClick = onRemove, enabled = enabled) { Text("取消互传位置") }
        else TextButton(onClick = onDismiss, enabled = enabled) { Text("返回") }
    })
}

@Composable
internal fun UploadDialog(prompt: CloudUploadPrompt, available: Boolean, onUpload: (Boolean) -> Unit, onDismiss: () -> Unit) {
    var keepBoth by remember(prompt) { mutableStateOf(false) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("上传 ${prompt.files.size} 个文件？") }, text = {
        Column(Modifier.heightIn(max = 260.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            SelectionContainer { Text("上传到：${breadcrumb(prompt.target.directory)}") }
            Text("保留手机原件，默认跳过云盘同名文件。")
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
                .toggleable(value = keepBoth, role = Role.Checkbox, onValueChange = { keepBoth = it }),
                verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = keepBoth, onCheckedChange = null, modifier = Modifier.padding(end = 8.dp))
                Text("同名时保留两份")
            }
        }
    }, confirmButton = { Button(onClick = { onUpload(keepBoth) }, enabled = available) { Text("开始上传") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } })
}

@Composable
internal fun UploadReportDialog(report: CloudUploadReport, work: CloudWork?, feedback: CloudFeedback?, onDismiss: (Boolean) -> Unit) {
    val created = report.files.count { it.state == UploadState.CREATED }
    val unknown = report.files.any { it.state == UploadState.UNKNOWN }
    val verify = unknown && report.refreshed == null
    AlertDialog(onDismissRequest = { if (work == null) onDismiss(false) }, title = { Text("上传结果 · $created/${report.files.size} 已上传") }, text = {
        LazyColumn(Modifier.heightIn(max = 300.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item { Text("目标：${breadcrumb(report.directory)}", maxLines = 3, overflow = TextOverflow.Ellipsis) }
            if (unknown) item {
                InlineCloudMessage(when {
                    work != null -> "正在读取云盘，不会重新上传。"
                    feedback?.error == true -> "刷新失败。先保留这份结果，再重试刷新。"
                    report.refreshed != null -> "列表已刷新。关闭结果后查看下列文件，未自动重传。"
                    else -> "部分结果未确认。先刷新核对云盘文件。"
                }, report.refreshed == null && work == null)
            } else if (created > 0 && report.refreshed == null) item { Text("已确认的文件无需重传；可刷新目录查看。") }
            items(report.files) { file ->
                Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                    Text(file.name, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    val detail = when (file.state) {
                        UploadState.CREATED -> if (file.savedAs == file.name) "已上传" else "另名上传：${file.savedAs}"
                        UploadState.SKIPPED -> "同名，已跳过"
                        UploadState.FAILED -> file.issue?.let { CloudFeedback(it).text } ?: "未上传"
                        UploadState.UNKNOWN -> "待核对：${file.savedAs ?: file.name}"
                        UploadState.CANCELLED -> "已取消此文件的上传"
                        UploadState.NOT_STARTED -> if (file.issue == CloudIssue.UPLOAD_UNCONFIRMED) "未尝试，上一项结果未确认"
                            else file.issue?.let { CloudFeedback(it).text } ?: "未开始"
                    }
                    Text(detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }, confirmButton = { TextButton(onClick = { onDismiss(verify) }, enabled = work == null) { Text(if (verify) "刷新核对" else "完成") } },
        dismissButton = { if (verify && work == null) TextButton(onClick = { onDismiss(false) }) { Text("关闭结果") } })
}

@Composable
internal fun CloudTransferStatus(work: CloudWork?, progress: Pair<Long, Long>?, upload: UploadProgress?,
                                 feedback: CloudFeedback?, onCancel: () -> Unit, onDismiss: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    Surface(color = if (feedback?.error == true) colors.errorContainer else colors.surfaceContainerHigh,
        contentColor = if (feedback?.error == true) colors.onErrorContainer else colors.onSurface,
        modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.navigationBarsPadding().padding(horizontal = 16.dp, vertical = 6.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp)) {
            val label = when {
                upload != null -> "第 ${upload.index}/${upload.totalFiles} 个文件 · " + when (upload.phase) {
                    UploadPhase.PREPARING -> "读取所选文件"
                    UploadPhase.SENDING -> "发送文件"
                    UploadPhase.CONFIRMING -> "等待云盘确认写入"
                }
                work == CloudWork.DOWNLOAD && progress?.let { it.first == it.second } == true -> "正在核对并保存"
                else -> work?.label ?: "等待选择文件"
            }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(feedback?.text ?: label, Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite },
                    style = MaterialTheme.typography.bodyMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (work?.foregroundOnly == true && work != CloudWork.SAVE_FILE) TextButton(onClick = onCancel) { Text("取消") }
                else if (feedback != null) TextButton(onClick = onDismiss) { Text("关闭") }
            }
            val activeTransfer = when {
                upload?.phase == UploadPhase.SENDING -> upload.totalBytes?.let { upload.bytes to it }
                work == CloudWork.DOWNLOAD && progress?.let { it.first < it.second } == true -> progress
                else -> null
            }
            if (activeTransfer != null) {
                val (count, total) = activeTransfer
                LinearProgressIndicator(progress = { (count.toDouble() / total.coerceAtLeast(1)).toFloat().coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth())
                val percent = (100.0 * count / total.coerceAtLeast(1)).toInt().coerceIn(0, 99)
                Text("$percent% · ${sizeLabel(count)} / ${sizeLabel(total)}", style = MaterialTheme.typography.bodySmall)
            } else if (work != null) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }
}
