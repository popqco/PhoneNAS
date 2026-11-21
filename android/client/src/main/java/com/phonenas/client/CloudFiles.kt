package com.phonenas.client

internal enum class CloudSetupStep { ADDRESS, IDENTITY, SIGN_IN }
internal enum class CloudTab { FILES, CONNECTION }

internal class CloudDirectory(val path: DavPath, val entries: List<DavEntry>, val needsRefresh: Boolean = false) {
    fun refreshFailed(requested: DavPath): CloudDirectory =
        if (requested == path) CloudDirectory(path, entries, needsRefresh = true) else this

    fun afterUpload(report: CloudUploadReport): CloudDirectory = when {
        report.directory != path -> this
        report.refreshed != null -> report.refreshed
        report.files.any { it.state == UploadState.CREATED || it.state == UploadState.UNKNOWN } -> refreshFailed(path)
        else -> this
    }

    fun canUpload(exchange: ExchangeShare?) = !needsRefresh && exchange?.contains(path) == true

    fun orderedEntries(): List<DavEntry> = entries.sortedWith(
        compareBy<DavEntry> { !it.path.isCollection }.thenBy(String.CASE_INSENSITIVE_ORDER) { it.path.name }
            .thenBy { it.path.name },
    )
}
