package com.phonenas.client

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
internal fun CloudFilesScreen(directory: CloudDirectory, exchange: ExchangeShare?, available: Boolean, work: CloudWork?,
                              rememberCurrent: Boolean, actions: CloudUiActions, modifier: Modifier) {
    val root = directory.path == DavPath.Root
    val entries = remember(directory) { directory.orderedEntries() }
    var file by remember(directory.path) { mutableStateOf<DavEntry?>(null) }
    key(directory.path) {
        LazyColumn(modifier, contentPadding = PaddingValues(start = 20.dp, top = 6.dp, end = 20.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)) {
            item(key = "location") {
                Column(Modifier.fillMaxWidth().padding(bottom = 6.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                    if (!root) {
                        SelectionContainer { Text(breadcrumb(directory.path), style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant) }
                    }
                    if (root && exchange != null) {
                        ExchangeShortcut(exchange.root.name, available) { actions.onList(exchange.root) }
                    }
                    if (directory.canUpload(exchange)) {
                        FilledTonalButton(onClick = actions.onChooseUploads, enabled = available,
                            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                            Icon(painterResource(R.drawable.ic_upload), null, Modifier.size(19.dp))
                            Text(stringResource(R.string.cloud_upload), Modifier.padding(start = 8.dp))
                        }
                    }
                    if (directory.needsRefresh) {
                        InlineCloudMessage("刷新没成功，下面是上次看到的内容。刷新后再上传。", true)
                    }
                    if (root && !rememberCurrent) TextButton(onClick = { actions.onTab(CloudTab.CONNECTION) }, enabled = available) {
                        Text("到「连接」保存，下次不用再填写")
                    }
                    Text(if (root) "共享位置 · ${entries.size} 项" else if (directory.needsRefresh) "上次列表 · ${entries.size} 项"
                        else "${entries.size} 项", style = MaterialTheme.typography.titleSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.semantics { heading() })
                }
            }
            if (entries.isEmpty() && work != CloudWork.LIST && !directory.needsRefresh) item(key = "empty") {
                Surface(color = MaterialTheme.colorScheme.surfaceContainer, shape = MaterialTheme.shapes.large,
                    modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        IconTile(R.drawable.ic_folder)
                        Text(if (root) "没有可浏览的共享" else "这里还没有文件", style = MaterialTheme.typography.titleMedium)
                        Text(if (directory.canUpload(exchange)) "用上方的「上传文件」添加文件。"
                            else "可以点右上角刷新查看。", style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
            items(entries, key = { it.path.encoded }) { entry ->
                FileRow(entry, available && (!directory.needsRefresh || entry.path.isCollection)) {
                    if (entry.path.isCollection) actions.onList(entry.path) else file = entry
                }
            }
        }
    }
    file?.takeIf { selected -> !directory.needsRefresh && directory.entries.any { it.path == selected.path } }?.let { selected ->
        FileActionDialog(selected, available, onDismiss = { file = null }, onDownload = {
            file = null
            actions.onDownload(selected)
        }, onSaveAs = {
            file = null
            actions.onSaveAs(selected)
        })
    }
}

@Composable
private fun ExchangeShortcut(name: String, enabled: Boolean, onClick: () -> Unit) {
    Surface(color = MaterialTheme.colorScheme.primaryContainer, contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        shape = MaterialTheme.shapes.large, modifier = Modifier.fillMaxWidth().clickable(enabled = enabled, role = Role.Button, onClick = onClick)) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Icon(painterResource(R.drawable.ic_upload), null, Modifier.size(24.dp))
            Column(Modifier.weight(1f)) {
                Text("互传", style = MaterialTheme.typography.titleMedium)
                Text("打开 $name", maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall)
            }
            Icon(painterResource(R.drawable.ic_chevron), null, Modifier.size(18.dp))
        }
    }
}

@Composable
private fun FileRow(entry: DavEntry, enabled: Boolean, onClick: () -> Unit) {
    val folder = entry.path.isCollection
    Surface(color = MaterialTheme.colorScheme.surface, shape = MaterialTheme.shapes.medium,
        modifier = Modifier.fillMaxWidth().clickable(enabled = enabled, role = Role.Button, onClick = onClick)) {
        Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            IconTile(if (folder) R.drawable.ic_folder else R.drawable.ic_file, 38)
            Column(Modifier.weight(1f)) {
                Text(entry.path.name, maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyLarge)
                Text(if (folder) "文件夹" else entry.size?.let(::sizeLabel) ?: "文件", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Icon(painterResource(if (folder) R.drawable.ic_chevron else R.drawable.ic_more), null, Modifier.size(19.dp))
        }
    }
}

@Composable
private fun FileActionDialog(entry: DavEntry, enabled: Boolean, onDismiss: () -> Unit,
                             onDownload: () -> Unit, onSaveAs: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss,
        icon = { Icon(painterResource(R.drawable.ic_file), null) },
        title = { Text("文件操作") },
        text = {
            Column(Modifier.heightIn(max = 320.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(entry.path.name)
                entry.size?.let { Text(sizeLabel(it), color = MaterialTheme.colorScheme.onSurfaceVariant) }
                HorizontalDivider()
                Button(onClick = onDownload, enabled = enabled, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Text("下载到手机")
                }
                OutlinedButton(onClick = onSaveAs, enabled = enabled,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("另存为") }
                Text("直接下载默认存到「下载 / PhoneNAS」。", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }, confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } })
}

internal fun breadcrumb(path: DavPath): String = generateSequence(path) { it.parent }
    .filter { it.parent != null }.toList().asReversed().joinToString(" / ", prefix = "云盘 / ") { it.name }

internal fun sizeLabel(bytes: Long): String = when {
    bytes < 1024L -> "$bytes B"
    bytes < 1024L * 1024 -> "${bytes / 1024} KiB"
    bytes < 1024L * 1024 * 1024 -> "${bytes / (1024L * 1024)} MiB"
    else -> "${bytes / (1024L * 1024 * 1024)} GiB"
}
