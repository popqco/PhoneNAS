package com.phonenas.client

import java.io.IOException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class DavListingTest {
    @Test fun listsRealGoVirtualRootAndHidesControlExport() {
        // Captured from the Go 0.4.2 engine, Depth:1 with just resourcetype/getcontentlength.
        val xml = multi(goCollection("/"), goCollection("/.phonenas/"), goCollection("/dataset/"))
        val root = DavPath.Root
        val dataset = parse(xml).single()
        assertEquals("/", root.name)
        assertEquals("/", root.encoded)
        assertTrue(root.isCollection)
        assertNull(root.parent)
        assertEquals("dataset", dataset.path.name)
        assertEquals("/dataset/", dataset.path.encoded)
        assertEquals(root, dataset.path.parent)
        assertTrue(dataset.path.isCollection)
        assertNull(dataset.size)
    }

    @Test fun listsRealGoAllpropWithLiteralPlusAndXmlEscaping() {
        // Empty PROPFIND body (the client's default). Mirror Go's nested DAV properties,
        // raw '+' and '&amp;' in href, and both file sizes from the captured fixture.
        val directory = parse(multi(response("/", true), response("/dataset/", true))).single().path
        val xml = """
            <?xml version="1.0" encoding="UTF-8"?><D:multistatus xmlns:D="DAV:">
            <D:response><D:href>/dataset/</D:href><D:propstat><D:prop>
            <D:resourcetype><D:collection xmlns:D="DAV:"/></D:resourcetype><D:displayname>dataset</D:displayname>
            <D:getlastmodified>Thu, 01 Jan 1970 00:00:00 GMT</D:getlastmodified>
            <D:supportedlock><D:lockentry xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>
            </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
            <D:response><D:href>/dataset/a&amp;b.txt</D:href><D:propstat><D:prop>
            <D:resourcetype></D:resourcetype><D:displayname>a&amp;b.txt</D:displayname>
            <D:getcontentlength>0</D:getcontentlength><D:getcontenttype>text/plain; charset=utf-8</D:getcontenttype>
            </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
            <D:response><D:href>/dataset/empty%20directory/</D:href><D:propstat><D:prop>
            <D:resourcetype><D:collection xmlns:D="DAV:"/></D:resourcetype><D:displayname>empty directory</D:displayname>
            </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
            <D:response><D:href>/dataset/%E4%B8%AD%E6%96%87%20space%25%23+.txt</D:href><D:propstat><D:prop>
            <D:resourcetype></D:resourcetype><D:displayname>中文 space%#+.txt</D:displayname><D:getcontentlength>10</D:getcontentlength>
            </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>
        """.trimIndent()
        val (ampersand, empty, unicode) = parse(xml, directory)
        assertEquals("a&b.txt", ampersand.path.name)
        assertEquals("/dataset/a%26b.txt", ampersand.path.encoded)
        assertEquals(0L, ampersand.size)
        assertEquals("empty directory", empty.path.name)
        assertEquals("/dataset/empty%20directory/", empty.path.encoded)
        assertNull(empty.size)
        assertEquals("中文 space%#+.txt", unicode.path.name)
        assertEquals("/dataset/%E4%B8%AD%E6%96%87%20space%25%23%2B.txt", unicode.path.encoded)
        assertFalse(unicode.path.isCollection)
        assertEquals(10L, unicode.size)
        assertEquals(directory, unicode.path.parent)
        assertTrue(parse(multi(goCollection("/dataset/empty%20directory/")), empty.path).isEmpty())
    }

    @Test fun onlyDecodedReservedNamesAreHidden() {
        val directory = parse(multi(response("/", true), response("/dataset/", true))).single().path
        val entries = parse(multi(
            response("/dataset/", true),
            response("/dataset/%2ePhOnEnAs/", true), response("/dataset/.PHONEnaS-temp-stage", false),
            response("/dataset/.phonenas.txt", false), response("/dataset/.phonenas-temp", false),
            response("/dataset/%252ephonenas", false), response("/dataset/%252F%25ZZ+a", false),
        ), directory)
        assertEquals(listOf(".phonenas.txt", ".phonenas-temp", "%2ephonenas", "%2F%ZZ+a"),
            entries.map { it.path.name })
        assertEquals("/dataset/%252ephonenas", entries[2].path.encoded)
        assertEquals("/dataset/%252F%25ZZ%2Ba", entries[3].path.encoded)
        assertNull(entries[3].size)
    }

    @Test fun sameDirectoryAndChildSpelledWithOtherEscapesHaveEqualPaths() {
        val first = parse(multi(response("/", true), response("/A+%F0%9F%8E%B5/", true))).single().path
        val second = parse(multi(response("/", true), response("/%41%2b%f0%9f%8e%b5/", true))).single().path
        assertEquals("A+🎵", first.name)
        assertEquals(first, second)
        assertEquals(first.hashCode(), second.hashCode())
        assertEquals("/A%2B%F0%9F%8E%B5/", first.encoded)
        val child = parse(multi(response("/%41+%f0%9f%8e%b5/sub/", true), response("/A%2B%F0%9F%8E%B5/", true)), first).single()
        assertEquals("/A%2B%F0%9F%8E%B5/sub/", child.path.encoded)
        assertEquals(first, child.path.parent)
    }

    @Test fun requiresSelfAndRejectsDuplicateOrUnrelatedResponses() {
        val directory = parse(multi(response("/", true), response("/dataset/", true))).single().path
        val self = response("/dataset/", true)
        reject(multi(), directory)
        reject(multi(response("/dataset/file", false)), directory)
        reject(multi(self, response("/%64ataset/", true)), directory)
        val wrong = listOf(
            response("/", true), response("/dataset2/child", false), response("/Dataset/child", false),
            response("/dataset/grand/child", false), response("/dataset/.phonenas/child", false),
            response("/other/.phonenas/", true), response("/dataset", false), response("/dataset/no-slash", true),
            response("/dataset/file/", false),
        )
        for (child in wrong) reject(multi(self, child), directory)
        reject(multi(self, response("/dataset/name", false), response("/dataset/%6Eame/", true)), directory)
        val file = parse(multi(self, response("/dataset/file", false)), directory).single().path
        reject(multi(response("/dataset/file", false)), file)
    }

    @Test fun rejectsMalformedOrForeignOriginRelativePaths() {
        val invalid = listOf(
            "https://other.invalid/file", "http://localhost/", "//other.invalid/file", "relative", "",
            "/file?q=1", "/file#fragment", "/raw space", "/raw中文", " /file", "/file ", "/raw\\path",
            "/.", "/..", "/%2e", "/%2E%2e", "/a%2fb", "/a%5Cb", "/a%00b", "/bad%GG", "/bad%1", "/bad%",
            "/%FF", "/%C0%AF", "/%E4%B8", "/%ED%A0%80", "/%F4%90%80%80",
        )
        for (href in invalid) reject(multi(response("/", true), response(href, false)))
        reject(multi(response("/", true), response("//", true)))
        val directory = parse(multi(response("/", true), response("/dataset/", true))).single().path
        reject(multi(response("/dataset/", true), response("/dataset//child", false)), directory)
    }

    @Test fun requiresDavNamespaceHierarchyAndSuccessfulResourceType() {
        val self = response("/", true)
        val wrong = listOf(
            multi(self).replace("DAV:", "not-DAV"),
            multi("<D:unrelated>$self</D:unrelated>"),
            multi(self.replace("<D:prop>", "<D:prop><D:displayname>${response("/hidden", false)}</D:displayname>")),
            multi(self.replace("<D:href>", "<D:href xmlns:D=\"not-DAV\">")),
            multi(self.replace("<D:href>/</D:href>", "<D:href>/</D:href><D:href>/</D:href>")),
            multi(self.replace("<D:href>/</D:href>", "<D:href><D:part>/</D:part></D:href>")),
            multi(self.replace("<D:href>", "<D:href xml:base=\"https://other.invalid\">")),
            multi(self.replace("HTTP/1.1 200 OK", "HTTP/1.1 404 Not Found")),
            multi(self.replace("HTTP/1.1 200 OK", "not HTTP 200")),
            multi(self.replace("<D:status>HTTP/1.1 200 OK</D:status>", "")),
            multi(self.replace("D:resourcetype", "D:displayname")),
            multi(self.replace("<D:collection/>", "<D:collection xmlns:D=\"not-DAV\"/>")),
            multi(self.replace("<D:collection/>", "<D:collection/><D:collection/>")),
            multi(self, "<D:response><D:href>/lost</D:href><D:status>HTTP/1.1 500 Failed</D:status></D:response>"),
            multi(self, response("/file", false).replace("HTTP/1.1 200 OK", "HTTP/1.1 404 Not Found")),
            multi(self, response("/file", false).replace("<D:resourcetype></D:resourcetype>", "<D:resourcetype/><D:resourcetype/>")),
        )
        for (xml in wrong) reject(xml)
        val unprefixed = multi(self).replace("xmlns:D", "xmlns").replace("D:", "")
        assertTrue(parse(unprefixed).isEmpty())
    }

    @Test fun validatesSizesWithoutSubstitutingFailedProperties() {
        val self = response("/", true)
        for (size in listOf("", "-1", "+1", "1.0", "1 0", "9223372036854775808", "１２")) {
            reject(multi(self, response("/file", false, size)))
        }
        assertEquals(Long.MAX_VALUE, parse(multi(self, response("/file", false, "9223372036854775807"))).single().size)
        val optional = response("/file", false).replace("</D:response>", """
            <D:propstat><D:prop><D:getcontentlength/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response>
        """.trimIndent())
        assertNull(parse(multi(self, optional)).single().size)
    }

    @Test fun rejectsDtdTruncationAndTrailingNonXml() {
        val self = response("/", true)
        val body = multi(self).substringAfter("?>")
        reject("<!DOCTYPE D:multistatus>$body")
        reject("<!DOCTYPE D:multistatus SYSTEM \"file:///never-read-this\">$body")
        reject("<!DOCTYPE D:multistatus [<!ENTITY sample '/'>]>${body.replace(">/<", ">&sample;<")}")
        reject("<!DOCTYPE D:multistatus [<!ENTITY sample SYSTEM 'file:///never-read-this'>]>${body.replace(">/<", ">&sample;<")}")
        reject(body.dropLast(1))
        reject(body + "unexpected")
        reject(body + body)
        val badText = multi(self, response("/file", false).replace("<D:prop>", "<D:prop><D:displayname>BAD</D:displayname>"))
        val bytes = badText.toByteArray().also { it[badText.indexOf("BAD")] = 0xff.toByte() }
        assertThrows(IOException::class.java) { DavListing.parse(DavPath.Root, bytes.inputStream()) }
    }

    @Test fun rejectsFieldTextDepthAndCountOveragesEvenAfterReadingSelf() {
        val root = response("/", true)
        reject(multi(root, response("/" + "x".repeat(16 * 1024), false)))
        val deep = "<D:displayname>" + "<D:level>".repeat(17) + "</D:level>".repeat(17) + "</D:displayname>"
        val lotsOfElements = "<D:displayname>" + "<D:element/>".repeat(100_000) + "</D:displayname>"
        for (property in listOf(deep, lotsOfElements)) {
            reject(multi(root, response("/file", false).replace("<D:prop>", "<D:prop>$property")))
        }
        val many = (1..10_000).map { response("/item$it", false) }
        assertEquals(9_999, parse(multi(root, *many.dropLast(1).toTypedArray())).size)
        reject(multi(root, *many.toTypedArray()))
    }

    @Test fun neverConvertsBodyOrTextOverageIntoAValidEmptyDirectory() {
        val body = multi(response("/", true))
        val text = "<D:displayname>" + "x".repeat(4 * 1024 * 1024) + "</D:displayname>"
        reject(body.replace("<D:prop>", "<D:prop>$text"))
        // A comment tail exercises the byte budget without the limit on character data.
        val comment = "<!--" + " ".repeat(8 * 1024 * 1024 - body.length - 7) + "-->"
        val atLimit = body + comment
        assertTrue(parse(atLimit).isEmpty())
        reject(atLimit + " ")
    }

    private fun parse(xml: String, directory: DavPath = DavPath.Root): List<DavEntry> =
        DavListing.parse(directory, xml.byteInputStream())

    private fun reject(xml: String, directory: DavPath = DavPath.Root) {
        assertThrows(xml.take(300), IOException::class.java) { parse(xml, directory) }
    }

    private fun multi(vararg responses: String): String =
        """<?xml version="1.0" encoding="UTF-8"?><D:multistatus xmlns:D="DAV:">${responses.joinToString("")}</D:multistatus>"""

    private fun goCollection(href: String): String =
        """<D:response><D:href>$href</D:href><D:propstat><D:prop><D:resourcetype><D:collection xmlns:D="DAV:"/></D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat><D:propstat><D:prop><D:getcontentlength></D:getcontentlength></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response>"""

    private fun response(href: String, collection: Boolean, size: String? = null): String {
        val type = if (collection) "<D:collection/>" else ""
        val length = size?.let { "<D:getcontentlength>$it</D:getcontentlength>" }.orEmpty()
        return "<D:response><D:href>$href</D:href><D:propstat><D:prop>" +
            "<D:resourcetype>$type</D:resourcetype>$length</D:prop>" +
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    }
}
