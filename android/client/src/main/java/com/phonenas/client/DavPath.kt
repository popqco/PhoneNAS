package com.phonenas.client

import java.nio.charset.CharacterCodingException

/** An origin-relative path made only from validated, decoded filename segments. */
internal class DavPath private constructor(
    val name: String,
    val parent: DavPath?,
    val isCollection: Boolean,
) {
    val encoded: String = if (parent == null) "/" else
        parent.encoded + encodeSegment(name) + (if (isCollection) "/" else "")
    private val segmentCount: Int = if (parent == null) 0 else parent.segmentCount + 1

    override fun equals(other: Any?): Boolean = other is DavPath && encoded == other.encoded
    override fun hashCode(): Int = encoded.hashCode()

    /** Returns the decoded name of a direct child, or null for the directory itself. */
    internal fun listedChildName(href: String, collection: Boolean): String? {
        if (!isCollection || !href.startsWith('/') || href.endsWith('/') != collection) invalidHref()
        val parts = if (href == "/") emptyList() else
            href.substring(1, href.length - if (collection) 1 else 0).split('/')
        if (parts.size !in segmentCount..(segmentCount + 1)) invalidHref()

        var expected = this
        for (index in segmentCount - 1 downTo 0) {
            if (decodeSegment(parts[index]) != expected.name) invalidHref()
            expected = checkNotNull(expected.parent)
        }
        if (parts.size == segmentCount) {
            if (!collection) invalidHref()
            return null
        }
        return decodeSegment(parts.last())
    }

    internal fun visibleChild(name: String, collection: Boolean): DavPath? {
        if (!isCollection || !validSegment(name)) invalidHref()
        val lower = name.lowercase()
        if (lower == ".phonenas" || lower.startsWith(".phonenas-temp-")) return null
        return DavPath(name, this, collection)
    }

    companion object {
        val Root: DavPath = DavPath("/", null, true)

        private const val HEX = "0123456789ABCDEF"
        private const val PATH_PUNCTUATION = "!$&'()*+,;=:@"

        private fun unreserved(c: Char): Boolean =
            c in 'a'..'z' || c in 'A'..'Z' || c in '0'..'9' || c in "-._~"

        private fun validSegment(name: String): Boolean = name.isNotEmpty() &&
            name != "." && name != ".." && name.none { it == '/' || it == '\\' || it == '\u0000' }

        private fun hexValue(c: Char): Int = when (c) {
            in '0'..'9' -> c - '0'
            in 'a'..'f' -> c - 'a' + 10
            in 'A'..'F' -> c - 'A' + 10
            else -> invalidHref()
        }

        private fun decodeSegment(raw: String): String {
            val bytes = ByteArray(raw.length)
            var i = 0
            var end = 0
            while (i < raw.length) {
                val c = raw[i++]
                bytes[end++] = if (c == '%') {
                    if (i + 1 >= raw.length) invalidHref()
                    ((hexValue(raw[i++]) shl 4) or hexValue(raw[i++])).toByte()
                } else {
                    if (!unreserved(c) && c !in PATH_PUNCTUATION) invalidHref()
                    c.code.toByte()
                }
            }
            val name = try {
                bytes.decodeToString(endIndex = end, throwOnInvalidSequence = true)
            } catch (_: CharacterCodingException) {
                invalidHref()
            }
            if (!validSegment(name)) invalidHref()
            return name
        }

        private fun encodeSegment(name: String): String {
            val bytes = try {
                name.encodeToByteArray(throwOnInvalidSequence = true)
            } catch (_: CharacterCodingException) {
                invalidHref()
            }
            return buildString {
                for (byte in bytes) {
                    val value = byte.toInt() and 0xff
                    if (unreserved(value.toChar())) append(value.toChar())
                    else append('%').append(HEX[value ushr 4]).append(HEX[value and 15])
                }
            }
        }

        private fun invalidHref(): Nothing = throw DavListingException("Invalid DAV listing path")
    }
}
