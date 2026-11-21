package com.phonenas.client

import android.content.Context
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import java.util.concurrent.Executors

internal class CloudDocument(private val context: Context) {
    fun save(uri: Uri, file: StagedDownload, operation: CloudOperation) {
        if (uri.scheme != "content" || !DocumentsContract.isDocumentUri(context, uri)) {
            throw CloudFailure(CloudIssue.SAVE)
        }
        val signal = CancellationSignal()
        try {
            operation.duringCancellation({ canceller.execute { signal.cancel() } }) {
                val descriptor = context.contentResolver.openFileDescriptor(uri, "rwt", signal)
                    ?: throw CloudFailure(CloudIssue.SAVE)
                ParcelFileDescriptor.AutoCloseOutputStream(descriptor).use { stream ->
                    if (descriptor.statSize < 0) throw CloudFailure(CloudIssue.SAVE)
                    file.copyTo(stream, operation::check)
                    operation.check()
                    if (descriptor.statSize != file.size) throw CloudFailure(CloudIssue.SAVE)
                    stream.fd.sync()
                    if (descriptor.canDetectErrors()) descriptor.checkError()
                    // Once sync, the provider check and close succeed, the file has been written.
                }
            }
        } catch (_: Exception) {
            // A provider may already have created or written a document; never delete it by guess.
            throw CloudFailure(CloudIssue.SAVE)
        }
    }

    private companion object {
        val canceller = Executors.newSingleThreadExecutor()
    }
}
