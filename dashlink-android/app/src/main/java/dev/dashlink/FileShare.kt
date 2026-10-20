package dev.dashlink

import android.webkit.MimeTypeMap
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.IHTTPSession
import fi.iki.elonen.NanoHTTPD.Method
import fi.iki.elonen.NanoHTTPD.Response
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Minimal WebDAV over the app's external files dir (models/, dashcam/, share/), mounted at [prefix].
 * Enough for rclone/davfs2 mounts and for HTTP range requests (seeking in dashcam video).
 */
class FileShare(val root: File, private val prefix: String = "/files") {

    init {
        File(root, "share").mkdirs()
    }

    fun handles(uri: String) = uri == prefix || uri.startsWith("$prefix/")

    fun serve(s: IHTTPSession): Response {
        // Any body we don't consume would be parsed as the next request on a keep-alive
        // connection, so everything except PUT drains it up front.
        if (s.method != Method.PUT) readFully(s)
        val file = resolve(s.uri) ?: return text(Response.Status.FORBIDDEN, "outside share")
        return when (s.method) {
            Method.OPTIONS -> options()
            Method.PROPFIND -> propfind(s, file)
            Method.GET, Method.HEAD -> get(s, file)
            Method.PUT -> put(s, file)
            Method.DELETE -> delete(file)
            Method.MKCOL -> mkcol(file)
            Method.MOVE, Method.COPY -> moveOrCopy(s, file, s.method == Method.MOVE)
            else -> text(Response.Status.METHOD_NOT_ALLOWED, "unsupported")
        }
    }

    private fun resolve(uri: String): File? {
        val rel = uri.removePrefix(prefix).trimStart('/')
        val f = File(root, rel).canonicalFile
        val base = root.canonicalFile
        return f.takeIf { it == base || it.path.startsWith(base.path + File.separator) }
    }

    private fun options() = NanoHTTPD.newFixedLengthResponse(Response.Status.OK, "text/plain", "").apply {
        addHeader("DAV", "1")
        addHeader("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL, MOVE, COPY")
        addHeader("MS-Author-Via", "DAV")
    }

    private fun propfind(s: IHTTPSession, file: File): Response {
        if (!file.exists()) return text(Response.Status.NOT_FOUND, "not found")
        val depth = s.headers["depth"] ?: "1"
        val entries = mutableListOf(file)
        if (file.isDirectory && depth != "0") entries += file.listFiles().orEmpty().sortedBy { it.name }
        val xml = StringBuilder("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:multistatus xmlns:D=\"DAV:\">\n")
        for (f in entries) {
            xml.append("<D:response><D:href>").append(href(f)).append("</D:href><D:propstat><D:prop>")
            xml.append("<D:displayname>").append(escape(if (f == root.canonicalFile) "" else f.name)).append("</D:displayname>")
            xml.append("<D:getlastmodified>").append(httpDate(f.lastModified())).append("</D:getlastmodified>")
            if (f.isDirectory) {
                xml.append("<D:resourcetype><D:collection/></D:resourcetype>")
            } else {
                xml.append("<D:resourcetype/>")
                xml.append("<D:getcontentlength>").append(f.length()).append("</D:getcontentlength>")
                xml.append("<D:getcontenttype>").append(mime(f)).append("</D:getcontenttype>")
            }
            xml.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>\n")
        }
        xml.append("</D:multistatus>\n")
        return NanoHTTPD.newFixedLengthResponse(MULTI_STATUS, "application/xml; charset=utf-8", xml.toString())
    }

    private fun get(s: IHTTPSession, file: File): Response {
        if (!file.exists()) return text(Response.Status.NOT_FOUND, "not found")
        if (file.isDirectory) {
            val listing = file.listFiles().orEmpty().sortedBy { it.name }.joinToString("\n") {
                (if (it.isDirectory) "${it.name}/" else "${it.name}\t${it.length()}")
            }
            return text(Response.Status.OK, listing + "\n")
        }
        val len = file.length()
        val range = s.headers["range"]?.let { parseRange(it, len) }
        val r = if (range == null) {
            NanoHTTPD.newFixedLengthResponse(Response.Status.OK, mime(file), FileInputStream(file), len)
        } else {
            val (start, end) = range
            if (start >= len) {
                return text(Response.Status.RANGE_NOT_SATISFIABLE, "bad range").apply { addHeader("Content-Range", "bytes */$len") }
            }
            val input = FileInputStream(file).apply { channel.position(start) }
            NanoHTTPD.newFixedLengthResponse(Response.Status.PARTIAL_CONTENT, mime(file), input, end - start + 1).apply {
                addHeader("Content-Range", "bytes $start-$end/$len")
            }
        }
        r.addHeader("Accept-Ranges", "bytes")
        r.addHeader("Last-Modified", httpDate(file.lastModified()))
        return r
    }

    /** "bytes=a-b", "bytes=a-", "bytes=-n" → inclusive (start, end). Multi-range isn't supported. */
    private fun parseRange(header: String, len: Long): Pair<Long, Long>? {
        val spec = header.removePrefix("bytes=").split(",").first().trim()
        val dash = spec.indexOf('-').takeIf { it >= 0 } ?: return null
        val a = spec.substring(0, dash)
        val b = spec.substring(dash + 1)
        return when {
            a.isEmpty() && b.isNotEmpty() -> (len - b.toLong()).coerceAtLeast(0) to len - 1
            a.isNotEmpty() -> a.toLong() to (b.toLongOrNull() ?: (len - 1)).coerceAtMost(len - 1)
            else -> null
        }
    }

    private fun put(s: IHTTPSession, file: File): Response {
        if (file.isDirectory) {
            readFully(s)
            return text(Response.Status.METHOD_NOT_ALLOWED, "is a directory")
        }
        val length = s.headers["content-length"]?.toLongOrNull()
            ?: return text(LENGTH_REQUIRED, "Content-Length required (chunked uploads unsupported)")
        file.parentFile?.mkdirs()
        val existed = file.exists()
        // Write beside the target and rename, so a dropped connection never leaves a half file.
        val tmp = File(file.parentFile, ".${file.name}.part")
        FileOutputStream(tmp).use { out ->
            val buf = ByteArray(256 shl 10)
            var left = length
            while (left > 0) {
                val n = s.inputStream.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                if (n < 0) break
                out.write(buf, 0, n)
                left -= n
            }
            if (left > 0) {
                tmp.delete()
                return text(Response.Status.BAD_REQUEST, "body ended early")
            }
        }
        if (!tmp.renameTo(file)) {
            tmp.delete()
            return text(Response.Status.INTERNAL_ERROR, "could not write")
        }
        return text(if (existed) Response.Status.NO_CONTENT else Response.Status.CREATED, "")
    }

    private fun delete(file: File): Response {
        if (file == root.canonicalFile) return text(Response.Status.FORBIDDEN, "won't delete the share root")
        if (!file.exists()) return text(Response.Status.NOT_FOUND, "not found")
        return if (file.deleteRecursively()) text(Response.Status.NO_CONTENT, "")
        else text(Response.Status.INTERNAL_ERROR, "delete failed")
    }

    private fun mkcol(file: File): Response = when {
        file.exists() -> text(Response.Status.METHOD_NOT_ALLOWED, "exists")
        file.parentFile?.isDirectory != true -> text(Response.Status.CONFLICT, "parent missing")
        file.mkdir() -> text(Response.Status.CREATED, "")
        else -> text(Response.Status.INTERNAL_ERROR, "mkdir failed")
    }

    private fun moveOrCopy(s: IHTTPSession, file: File, move: Boolean): Response {
        if (!file.exists()) return text(Response.Status.NOT_FOUND, "not found")
        val destHeader = s.headers["destination"] ?: return text(Response.Status.BAD_REQUEST, "Destination required")
        val destPath = URLDecoder.decode(URI(destHeader).rawPath, "UTF-8")
        val dest = resolve(destPath) ?: return text(Response.Status.FORBIDDEN, "outside share")
        val overwrite = s.headers["overwrite"] != "F"
        val existed = dest.exists()
        if (existed && !overwrite) return text(PRECONDITION_FAILED, "destination exists")
        if (existed) dest.deleteRecursively()
        dest.parentFile?.mkdirs()
        val ok = if (move) file.renameTo(dest) else runCatching { file.copyRecursively(dest, overwrite = true) }.getOrDefault(false)
        return if (ok) text(if (existed) Response.Status.NO_CONTENT else Response.Status.CREATED, "")
        else text(Response.Status.INTERNAL_ERROR, "failed")
    }

    private fun href(f: File): String {
        val rel = f.canonicalPath.removePrefix(root.canonicalPath).trimStart('/')
        val encoded = rel.split('/').filter { it.isNotEmpty() }.joinToString("/") {
            URLEncoder.encode(it, "UTF-8").replace("+", "%20")
        }
        val path = if (encoded.isEmpty()) "$prefix/" else "$prefix/$encoded"
        return if (f.isDirectory && !path.endsWith("/")) "$path/" else path
    }

    private fun readFully(s: IHTTPSession) {
        var left = s.headers["content-length"]?.toLongOrNull() ?: return
        val buf = ByteArray(8192)
        while (left > 0) {
            val n = s.inputStream.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
            if (n < 0) break
            left -= n
        }
    }

    private fun mime(f: File) =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.extension.lowercase()) ?: "application/octet-stream"

    private fun escape(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    private fun httpDate(ms: Long) = SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss 'GMT'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("GMT") }.format(Date(ms))

    private fun text(status: Response.IStatus, body: String) =
        NanoHTTPD.newFixedLengthResponse(status, "text/plain", body)

    private class Status(private val code: Int, private val text: String) : Response.IStatus {
        override fun getDescription() = "$code $text"
        override fun getRequestStatus() = code
    }

    companion object {
        private val MULTI_STATUS: Response.IStatus = Status(207, "Multi-Status")
        private val LENGTH_REQUIRED: Response.IStatus = Status(411, "Length Required")
        private val PRECONDITION_FAILED: Response.IStatus = Status(412, "Precondition Failed")
    }
}
