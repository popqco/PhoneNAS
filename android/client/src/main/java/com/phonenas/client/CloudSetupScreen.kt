package com.phonenas.client

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp

@Composable
internal fun CloudSetupScreen(
    step: CloudSetupStep,
    address: String,
    fingerprint: String,
    username: String,
    password: String,
    hasCertificate: Boolean,
    enabled: Boolean,
    feedback: CloudFeedback?,
    onAddress: (String) -> Unit,
    onFingerprint: (String) -> Unit,
    onUsername: (String) -> Unit,
    onPassword: (String) -> Unit,
    onImport: () -> Unit,
    onContinue: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val (number, title) = when (step) {
        CloudSetupStep.ADDRESS -> 1 to "添加云盘"
        CloudSetupStep.IDENTITY -> 2 to "验证云盘"
        CloudSetupStep.SIGN_IN -> 3 to "登录云盘"
    }
    val ready = when (step) {
        CloudSetupStep.ADDRESS -> address.isNotBlank()
        CloudSetupStep.IDENTITY -> hasCertificate && fingerprint.isNotBlank()
        CloudSetupStep.SIGN_IN -> username.isNotBlank() && password.isNotEmpty()
    }
    val focus = LocalFocusManager.current
    var revealPassword by remember(step) { mutableStateOf(false) }
    val submit = {
        focus.clearFocus()
        if (enabled && ready) onContinue()
    }
    val keyboardDone = KeyboardActions(onDone = { submit() })

    key(step) {
        Column(
            modifier.fillMaxSize().imePadding().verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("第 $number 步，共 3 步", style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.primary)
                Text(title, Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineSmall)
            }
            when (step) {
                CloudSetupStep.ADDRESS -> {
                    CloudSetupHint("先确认两台手机已连接 Tailscale，再填写云盘手机的 PhoneNAS 首页显示的 HTTPS 地址或 Tailscale IPv4。")
                    OutlinedTextField(value = address, onValueChange = onAddress, enabled = enabled,
                        label = { Text("云盘地址") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Done),
                        keyboardActions = keyboardDone)
                }
                CloudSetupStep.IDENTITY -> {
                    CloudSetupHint("从云盘手机首页导出公开证书。连接时会核对云盘出示的证书是否与它一致。")
                    OutlinedButton(
                        onClick = { focus.clearFocus(); onImport() }, enabled = enabled,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                    ) {
                        Text(if (hasCertificate) "公开证书已选 · 重新选择" else "导入公开证书")
                    }
                    OutlinedTextField(value = fingerprint, onValueChange = onFingerprint, enabled = enabled,
                        label = { Text("证书指纹") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                        supportingText = { Text("对照云盘手机首页的 SHA-256 指纹填写，用来确认导入的是这台云盘的证书。") },
                        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, imeAction = ImeAction.Done),
                        keyboardActions = keyboardDone)
                }
                CloudSetupStep.SIGN_IN -> {
                    CloudSetupHint("使用云盘手机上设置的用户名和密码。")
                    OutlinedTextField(value = username, onValueChange = onUsername, enabled = enabled,
                        label = { Text("用户名") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, imeAction = ImeAction.Next))
                    OutlinedTextField(value = password, onValueChange = onPassword, enabled = enabled,
                        label = { Text("密码") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                        visualTransformation = if (revealPassword) VisualTransformation.None else PasswordVisualTransformation(),
                        trailingIcon = {
                            IconButton(onClick = { revealPassword = !revealPassword }, enabled = enabled) {
                                Icon(
                                    painterResource(if (revealPassword) R.drawable.ic_visibility_off else R.drawable.ic_visibility),
                                    stringResource(if (revealPassword) R.string.cloud_hide_password else R.string.cloud_show_password),
                                )
                            }
                        },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                        keyboardActions = keyboardDone)
                }
            }
            feedback?.let { CloudSetupFeedback(it) }
            Button(
                onClick = submit, enabled = enabled && ready,
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
            ) {
                Text(if (step == CloudSetupStep.SIGN_IN) "连接" else "继续")
            }
        }
    }
}

@Composable
private fun CloudSetupHint(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun CloudSetupFeedback(feedback: CloudFeedback) {
    val colors = MaterialTheme.colorScheme
    Surface(
        modifier = Modifier.fillMaxWidth(), shape = MaterialTheme.shapes.medium,
        color = if (feedback.error) colors.errorContainer else colors.surfaceVariant,
        contentColor = if (feedback.error) colors.onErrorContainer else colors.onSurfaceVariant,
    ) {
        Text(feedback.text, Modifier.padding(12.dp).semantics { liveRegion = LiveRegionMode.Polite },
            style = MaterialTheme.typography.bodyMedium)
    }
}
