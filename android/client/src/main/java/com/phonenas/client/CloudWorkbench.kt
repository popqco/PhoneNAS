package com.phonenas.client

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

internal class CloudUiState(
    val tab: CloudTab, val step: CloudSetupStep?, val directory: CloudDirectory?, val connectedUrl: String?,
    val savedHost: String?, val rememberCurrent: Boolean, val profileUnreadable: Boolean, val exchange: ExchangeShare?,
    val work: CloudWork?, val choosingFile: Boolean, val choosingExchange: Boolean, val askingForget: Boolean,
    val feedback: CloudFeedback?, val progress: Pair<Long, Long>?, val uploadProgress: UploadProgress?,
    val uploadPrompt: CloudUploadPrompt?, val uploadReport: CloudUploadReport?,
) {
    val idle = work == null && !choosingFile
    val available = idle && !choosingExchange && !askingForget && uploadPrompt == null && uploadReport == null
}

internal class CloudSetupFields(
    val address: String, val fingerprint: String, val username: String, val password: String, val hasCertificate: Boolean,
)

internal class CloudSetupActions(
    val onAddress: (String) -> Unit, val onFingerprint: (String) -> Unit, val onUsername: (String) -> Unit,
    val onPassword: (String) -> Unit, val onImport: () -> Unit, val onContinue: () -> Unit,
)

internal class CloudUiActions(
    val onTab: (CloudTab) -> Unit, val onConnect: () -> Unit, val onEdit: () -> Unit, val onBack: () -> Unit,
    val onList: (DavPath) -> Unit, val onDownload: (DavEntry) -> Unit, val onSaveAs: (DavEntry) -> Unit,
    val onChooseExchange: () -> Unit, val onDismissExchange: () -> Unit, val onExchange: (DavPath) -> Unit,
    val onRemoveExchange: () -> Unit, val onChooseUploads: () -> Unit, val onUpload: (Boolean) -> Unit,
    val onDismissUpload: () -> Unit, val onDismissReport: (Boolean) -> Unit, val onRemember: () -> Unit,
    val onAskForget: () -> Unit, val onForget: () -> Unit, val onDismissForget: () -> Unit,
    val onCancel: () -> Unit, val onDismissFeedback: () -> Unit,
)

private val light = lightColorScheme(
    primary = Color(0xff185e72), onPrimary = Color.White,
    primaryContainer = Color(0xffd5eef5), onPrimaryContainer = Color(0xff082f3b),
    secondary = Color(0xff445e68), secondaryContainer = Color(0xffdeedf1), onSecondaryContainer = Color(0xff1b3841),
    background = Color(0xfff5f8fa), onBackground = Color(0xff172c35),
    surface = Color(0xfffbfdfe), onSurface = Color(0xff172c35),
    surfaceVariant = Color(0xffe4eef2), onSurfaceVariant = Color(0xff425863),
    surfaceContainer = Color(0xffedf4f7), surfaceContainerHigh = Color(0xffe6f0f4),
    outline = Color(0xff687f8b), outlineVariant = Color(0xffcedde3),
    error = Color(0xffa12a33), errorContainer = Color(0xffffe8e7), onErrorContainer = Color(0xff741c24),
)

private val dark = darkColorScheme(
    primary = Color(0xff9fdaed), onPrimary = Color(0xff003642),
    primaryContainer = Color(0xff204e5d), onPrimaryContainer = Color(0xffd2eef6),
    secondary = Color(0xffb6d1db), secondaryContainer = Color(0xff294653), onSecondaryContainer = Color(0xffd9eaf0),
    background = Color(0xff101b23), onBackground = Color(0xffe0ecf1),
    surface = Color(0xff13232d), onSurface = Color(0xffe0ecf1),
    surfaceVariant = Color(0xff213945), onSurfaceVariant = Color(0xffbdd1da),
    surfaceContainer = Color(0xff192b36), surfaceContainerHigh = Color(0xff1f3541),
    outline = Color(0xff8ca9b6), outlineVariant = Color(0xff38525f),
    error = Color(0xffffb4b7), errorContainer = Color(0xff54242b), onErrorContainer = Color(0xffffdae0),
)

@Composable
internal fun CloudWorkbench(state: CloudUiState, credentials: CloudSetupFields, actions: CloudUiActions,
                            setup: CloudSetupActions) {
    val inFolder = state.tab == CloudTab.FILES && state.directory?.path?.parent != null
    BackHandler(enabled = state.step != null || state.tab != CloudTab.FILES || inFolder || state.work?.foregroundOnly == true) {
        actions.onBack()
    }
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) dark else light) {
        Scaffold(
            topBar = { CloudToolbar(state, actions) },
            bottomBar = {
                Column {
                    if (state.work != null || state.choosingFile || state.step == null && state.feedback != null) {
                        CloudTransferStatus(state.work, state.progress, state.uploadProgress,
                            state.feedback.takeIf { state.step == null }, actions.onCancel, actions.onDismissFeedback)
                    }
                    if (state.step == null) CloudNavigation(state.tab, state.available, actions.onTab)
                }
            },
        ) { insets ->
            val content = Modifier.fillMaxSize().padding(insets)
            val step = state.step
            when {
                step != null -> CloudSetupScreen(step, credentials.address, credentials.fingerprint, credentials.username,
                    credentials.password, credentials.hasCertificate, state.available, state.feedback,
                    setup.onAddress, setup.onFingerprint, setup.onUsername, setup.onPassword, setup.onImport,
                    setup.onContinue, content)
                state.tab == CloudTab.CONNECTION -> ConnectionScreen(state, actions, content)
                state.directory != null -> CloudFilesScreen(state.directory, state.exchange, state.available, state.work,
                    state.rememberCurrent, actions, content)
                else -> FilesWelcome(state, actions, content)
            }
        }
        if (state.choosingExchange) {
            val choices = state.directory?.orderedEntries().orEmpty().filter { ExchangeShare.selectable(it.path) }
            ExchangeDialog(choices, state.exchange, state.idle, state.feedback,
                actions.onExchange, actions.onRemoveExchange, actions.onDismissExchange)
        }
        state.uploadPrompt?.let { UploadDialog(it, state.idle, actions.onUpload, actions.onDismissUpload) }
        state.uploadReport?.let { UploadReportDialog(it, state.work, state.feedback, actions.onDismissReport) }
        if (state.askingForget) AlertDialog(onDismissRequest = actions.onDismissForget,
            title = { Text("移除本机连接？") },
            text = { Text("只移除这台手机上的连接资料，不改云盘，也不删除已下载的文件。") },
            confirmButton = { TextButton(onClick = actions.onForget) { Text("移除连接") } },
            dismissButton = { TextButton(onClick = actions.onDismissForget) { Text("取消") } })
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CloudToolbar(state: CloudUiState, actions: CloudUiActions) {
    val directory = state.directory.takeIf { state.step == null && state.tab == CloudTab.FILES }
    TopAppBar(
        title = {
            Text(when {
                state.step != null -> stringResource(R.string.cloud_connect)
                state.tab == CloudTab.CONNECTION -> stringResource(R.string.cloud_connection)
                directory == null -> stringResource(R.string.cloud_files)
                directory.path == DavPath.Root -> stringResource(R.string.cloud_all_shares)
                else -> directory.path.name
            }, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.semantics { heading() })
        },
        navigationIcon = {
            if (state.step != null || directory?.path?.parent != null) {
                IconButton(onClick = actions.onBack, enabled = state.available) {
                    Icon(painterResource(R.drawable.ic_back), stringResource(R.string.cloud_back))
                }
            }
        },
        actions = {
            if (directory != null) IconButton(onClick = { actions.onList(directory.path) }, enabled = state.available) {
                Icon(painterResource(R.drawable.ic_refresh), stringResource(R.string.cloud_refresh))
            }
        },
        colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
    )
}

@Composable
private fun CloudNavigation(tab: CloudTab, enabled: Boolean, onTab: (CloudTab) -> Unit) {
    NavigationBar(containerColor = MaterialTheme.colorScheme.surfaceContainer) {
        NavigationBarItem(selected = tab == CloudTab.FILES, onClick = { onTab(CloudTab.FILES) }, enabled = enabled,
            icon = { Icon(painterResource(R.drawable.ic_folder), null) }, label = { Text(stringResource(R.string.cloud_files)) })
        NavigationBarItem(selected = tab == CloudTab.CONNECTION, onClick = { onTab(CloudTab.CONNECTION) }, enabled = enabled,
            icon = { Icon(painterResource(R.drawable.ic_link), null) }, label = { Text(stringResource(R.string.cloud_connection)) })
    }
}

@Composable
private fun FilesWelcome(state: CloudUiState, actions: CloudUiActions, modifier: Modifier) {
    Column(modifier.verticalScroll(rememberScrollState()).padding(horizontal = 24.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp)) {
        Surface(color = MaterialTheme.colorScheme.primaryContainer, contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                IconTile(R.drawable.ic_cloud, 56)
                val title = when {
                    state.work == CloudWork.LOAD -> "正在准备文件"
                    state.profileUnreadable -> "本机连接无法读取"
                    state.savedHost != null -> "我的云盘"
                    else -> "开始使用 PhoneNAS"
                }
                Text(title, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                Text(when {
                    state.work == CloudWork.LOAD -> "正在读取这台手机上的连接。"
                    state.profileUnreadable -> "在「连接」里移除旧资料，再重新连接。"
                    state.savedHost != null -> "用本机已保存的连接打开共享文件。"
                    else -> "连接存放文件的手机，在这里查看、下载或上传文件。"
                }, style = MaterialTheme.typography.bodyLarge)
                if (state.work != CloudWork.LOAD) Button(
                    onClick = if (state.profileUnreadable) ({ actions.onTab(CloudTab.CONNECTION) }) else actions.onConnect,
                    enabled = state.available, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                ) { Text(when {
                    state.profileUnreadable -> "管理连接"
                    state.savedHost != null -> "打开云盘"
                    else -> stringResource(R.string.cloud_connect)
                }) }
            }
        }
        if (!state.profileUnreadable && state.savedHost == null && state.work != CloudWork.LOAD) {
            Text("出门也能用", style = MaterialTheme.typography.titleSmall)
            Text("两台手机都连上 Tailscale，再用云盘手机提供的安全 HTTPS 地址连接。电脑不用一直开着。",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun ConnectionScreen(state: CloudUiState, actions: CloudUiActions, modifier: Modifier) {
    val address = state.connectedUrl ?: state.savedHost?.let { "https://$it:18889" }
    Column(modifier.verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Surface(color = MaterialTheme.colorScheme.surfaceContainer, shape = MaterialTheme.shapes.extraLarge,
            modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                IconTile(R.drawable.ic_link, 48)
                Text(when {
                    state.profileUnreadable -> "连接资料不可读"
                    state.connectedUrl != null -> "当前云盘"
                    state.savedHost != null -> "已保存的连接"
                    else -> "尚未连接云盘"
                }, style = MaterialTheme.typography.titleLarge, modifier = Modifier.semantics { heading() })
                if (address != null) SelectionContainer { Text(address, style = MaterialTheme.typography.bodyMedium) }
                Text(if (state.rememberCurrent || state.connectedUrl == null && state.savedHost != null)
                    "连接资料已加密保存在本机。" else "连接资料不会自动保存。",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (state.connectedUrl != null && !state.rememberCurrent) Button(onClick = actions.onRemember,
                    enabled = state.available, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (state.savedHost == null) "保存连接到本机" else "保存连接设置")
                } else if (state.connectedUrl == null && !state.profileUnreadable) Button(onClick = actions.onConnect,
                    enabled = state.available, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (state.savedHost == null) stringResource(R.string.cloud_connect) else "打开云盘")
                }
            }
        }
        if (state.connectedUrl != null) SettingsRow("互传位置", state.exchange?.root?.name ?: "未设置 · 请选择独立的可写共享",
            state.available, actions.onChooseExchange)
        if (address != null) SettingsRow("重新配对", "更换云盘地址、公开证书或登录信息", state.available, actions.onEdit)
        if (state.profileUnreadable || state.savedHost != null || state.connectedUrl != null) {
            SettingsRow("移除本机连接", "不删除云盘或本机已下载的文件", state.available, actions.onAskForget)
        }
        HorizontalDivider()
        Text("连接要求", style = MaterialTheme.typography.titleSmall)
        Text("通过 Tailscale 访问 PhoneNAS 的 HTTPS 服务。仅提供 HTTP 的旧 WebDAV 不能直接配对。",
            color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun SettingsRow(title: String, detail: String, enabled: Boolean, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 60.dp).clickable(enabled = enabled, onClick = onClick)
        .padding(horizontal = 8.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Icon(painterResource(R.drawable.ic_chevron), null, Modifier.size(18.dp))
    }
}

@Composable
internal fun IconTile(icon: Int, size: Int = 40) {
    Surface(color = MaterialTheme.colorScheme.primaryContainer, contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        shape = MaterialTheme.shapes.medium, modifier = Modifier.size(size.dp)) {
        Box(contentAlignment = Alignment.Center) { Icon(painterResource(icon), null, Modifier.size(23.dp)) }
    }
}

@Composable
internal fun InlineCloudMessage(text: String, error: Boolean, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    Surface(color = if (error) colors.errorContainer else colors.secondaryContainer,
        contentColor = if (error) colors.onErrorContainer else colors.onSecondaryContainer,
        shape = MaterialTheme.shapes.medium, modifier = modifier.fillMaxWidth()) {
        Text(text, Modifier.padding(14.dp).semantics { liveRegion = LiveRegionMode.Polite },
            style = MaterialTheme.typography.bodyMedium)
    }
}
