package com.phonenas.client

import java.io.IOException
import java.io.InputStream
import java.util.ArrayDeque
import javax.xml.parsers.ParserConfigurationException
import javax.xml.parsers.SAXParserFactory
import org.xml.sax.Attributes
import org.xml.sax.InputSource
import org.xml.sax.SAXException
import org.xml.sax.SAXParseException
import org.xml.sax.ext.DefaultHandler2

internal data class DavEntry(val path: DavPath, val size: Long?)

internal class DavListingException(message: String) : IOException(message)

internal object DavListing {
    /** Reads the entire XML document before publishing any entries. The caller owns [input]. */
    @Throws(IOException::class)
    fun parse(directory: DavPath, input: InputStream): List<DavEntry> {
        if (!directory.isCollection) invalid("Not a DAV collection")
        val handler = ListingHandler(directory)
        try {
            val reader = SAXParserFactory.newInstance().apply {
                isNamespaceAware = true
                isValidating = false
            }.newSAXParser().xmlReader
            reader.setFeature("http://xml.org/sax/features/external-general-entities", false)
            reader.setFeature("http://xml.org/sax/features/external-parameter-entities", false)
            // Both Android Expat and the JVM support this property. Reject DTDs as soon as
            // they start, before either the internal subset or any external entity is parsed.
            reader.setProperty("http://xml.org/sax/properties/lexical-handler", handler)
            reader.entityResolver = handler
            reader.contentHandler = handler
            reader.errorHandler = handler
            reader.parse(InputSource(BoundedInput(input)).apply { encoding = "UTF-8" })
        } catch (_: SAXException) {
            invalid("Malformed or unsupported DAV XML")
        } catch (_: ParserConfigurationException) {
            invalid("Could not configure secure DAV XML parser")
        }
        return handler.result()
    }

    private const val DAV = "DAV:"
    private const val MAX_BYTES = 8 * 1024 * 1024
    private const val MAX_TEXT = 4 * 1024 * 1024
    private const val MAX_FIELD_TEXT = 16 * 1024
    private const val MAX_DEPTH = 16
    private const val MAX_ELEMENTS = 100_000
    private const val MAX_RESPONSES = 10_000
    private val STATUS_LINE = Regex("HTTP/1\\.[01] ([1-5][0-9]{2}) [\\x20-\\x7e]+")

    private class BoundedInput(private val source: InputStream) : InputStream() {
        private var remaining = MAX_BYTES

        override fun read(): Int {
            val byte = source.read()
            if (byte != -1 && remaining-- == 0) invalid("DAV listing byte limit exceeded")
            return byte
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            // Check one extra byte so reaching the limit is never mistaken for EOF.
            val count = source.read(buffer, offset, minOf(length, remaining + 1))
            if (count > remaining) invalid("DAV listing byte limit exceeded")
            if (count > 0) remaining -= count
            return count
        }
    }

    private enum class Element {
        MULTISTATUS, RESPONSE, HREF, PROPSTAT, PROP, STATUS, TYPE, COLLECTION, SIZE, IGNORE
    }

    private class Frame(val element: Element) {
        val text = when (element) {
            Element.HREF, Element.STATUS, Element.SIZE -> StringBuilder()
            else -> null
        }
    }

    private class Response {
        var href: String? = null
        var seenType = false
        var seenSize = false
        var collection: Boolean? = null
        var size: Long? = null
    }

    private class Propstat {
        var seenProp = false
        var status: Int? = null
        var collection: Boolean? = null
        var sizeText: String? = null
    }

    private class ListingHandler(private val directory: DavPath) : DefaultHandler2() {
        private val stack = ArrayDeque<Frame>()
        private val entries = ArrayList<DavEntry>()
        private val childNames = HashSet<String>()
        private var seenSelf = false
        private var elementCount = 0
        private var responseCount = 0
        private var textCount = 0
        private lateinit var response: Response
        private lateinit var propstat: Propstat

        fun result(): List<DavEntry> {
            if (!seenSelf) invalid("Missing DAV listing self response")
            return entries.toList()
        }

        override fun startElement(uri: String, localName: String, qName: String, attributes: Attributes) {
            if (stack.size >= MAX_DEPTH || ++elementCount > MAX_ELEMENTS) invalid("DAV XML limit exceeded")
            val parent = stack.peekLast()?.element
            val dav = localName.takeIf { uri == DAV }
            // Structural DAV tokens are never valid property content. Do not turn a
            // misplaced response hidden in an ignored subtree into an empty folder.
            if (dav == "multistatus" && parent != null || dav == "response" && parent != Element.MULTISTATUS) {
                invalid("Misplaced DAV listing element")
            }
            val element = when (parent) {
                null -> {
                    if (elementCount != 1 || dav != "multistatus") invalid("Expected DAV multistatus")
                    Element.MULTISTATUS
                }
                Element.MULTISTATUS -> {
                    if (dav != "response") invalid("Expected DAV response")
                    if (++responseCount > MAX_RESPONSES) invalid("DAV response limit exceeded")
                    response = Response()
                    Element.RESPONSE
                }
                Element.RESPONSE -> when (dav) {
                    "href" -> {
                        if (response.href != null) invalid("Duplicate DAV href")
                        Element.HREF
                    }
                    "propstat" -> {
                        propstat = Propstat()
                        Element.PROPSTAT
                    }
                    else -> invalid("Unexpected element in DAV response")
                }
                Element.PROPSTAT -> when (dav) {
                    "prop" -> {
                        if (propstat.seenProp) invalid("Duplicate DAV prop")
                        propstat.seenProp = true
                        Element.PROP
                    }
                    "status" -> {
                        if (propstat.status != null) invalid("Duplicate DAV status")
                        Element.STATUS
                    }
                    else -> invalid("Unexpected element in DAV propstat")
                }
                Element.PROP -> when (dav) {
                    "resourcetype" -> {
                        if (response.seenType) invalid("Duplicate DAV resourcetype")
                        response.seenType = true
                        propstat.collection = false
                        Element.TYPE
                    }
                    "getcontentlength" -> {
                        if (response.seenSize) invalid("Duplicate DAV getcontentlength")
                        response.seenSize = true
                        Element.SIZE
                    }
                    // Go's allprop includes irrelevant properties; don't interpret their subtrees.
                    else -> Element.IGNORE
                }
                Element.TYPE -> {
                    if (dav != "collection" || propstat.collection == true) invalid("Unexpected DAV resource type")
                    propstat.collection = true
                    Element.COLLECTION
                }
                Element.IGNORE -> Element.IGNORE
                else -> invalid("Unexpected element in DAV value")
            }
            // No xml:base or other modifiers on the fields used for the listing.
            if (element != Element.IGNORE && attributes.length != 0) invalid("Unexpected DAV attribute")
            stack.addLast(Frame(element))
        }

        override fun characters(chars: CharArray, start: Int, length: Int) {
            textCount += length
            if (textCount > MAX_TEXT) invalid("DAV XML text limit exceeded")
            val frame = stack.peekLast()
            if (frame?.element == Element.IGNORE) return
            val text = frame?.text
            if (text != null) {
                if (text.length + length > MAX_FIELD_TEXT) invalid("DAV field text limit exceeded")
                text.append(chars, start, length)
            } else {
                for (i in start until start + length) {
                    if (!xmlSpace(chars[i])) invalid("Unexpected DAV XML text")
                }
            }
        }

        override fun endElement(uri: String, localName: String, qName: String) {
            val frame = stack.removeLast()
            when (frame.element) {
                Element.HREF -> response.href = frame.text.toString()
                Element.STATUS -> propstat.status = STATUS_LINE.matchEntire(frame.text.toString().trim(::xmlSpace))
                    ?.groupValues?.get(1)?.toInt() ?: invalid("Invalid DAV property status")
                Element.SIZE -> propstat.sizeText = frame.text.toString()
                Element.PROPSTAT -> finishPropstat()
                Element.RESPONSE -> finishResponse()
                else -> Unit
            }
        }

        private fun finishPropstat() {
            val status = propstat.status ?: invalid("Missing DAV property status")
            if (!propstat.seenProp) invalid("Missing DAV prop")
            if (status !in 200..299) return
            propstat.collection?.let { response.collection = it }
            propstat.sizeText?.let {
                val value = it.trim(::xmlSpace)
                if (value.isEmpty() || value.any { c -> c !in '0'..'9' }) invalid("Invalid DAV file size")
                response.size = value.toLongOrNull() ?: invalid("Invalid DAV file size")
            }
        }

        private fun finishResponse() {
            val href = response.href ?: invalid("Missing DAV href")
            val collection = response.collection ?: invalid("Missing successful DAV resourcetype")
            val name = directory.listedChildName(href, collection)
            if (name == null) {
                if (seenSelf) invalid("Duplicate DAV listing self response")
                seenSelf = true
            } else {
                if (!childNames.add(name)) invalid("Duplicate DAV listing child")
                val child = directory.visibleChild(name, collection) ?: return
                entries.add(DavEntry(child, if (collection) null else response.size))
            }
        }

        override fun startDTD(name: String?, publicId: String?, systemId: String?) =
            invalid("DAV DTDs are not allowed")

        override fun resolveEntity(name: String?, publicId: String?, baseURI: String?, systemId: String?): InputSource =
            invalid("DAV external entities are not allowed")

        override fun skippedEntity(name: String?) = invalid("DAV entities are not allowed")
        override fun error(e: SAXParseException) { throw e }
        override fun fatalError(e: SAXParseException) { throw e }
    }

    private fun xmlSpace(c: Char): Boolean = c == ' ' || c == '\t' || c == '\r' || c == '\n'
    private fun invalid(message: String): Nothing = throw DavListingException(message)
}
