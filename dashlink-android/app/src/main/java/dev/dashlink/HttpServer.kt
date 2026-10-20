package dev.dashlink

import android.content.Context
import fi.iki.elonen.NanoHTTPD
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.InputStream

class HttpServer(
    private val context: Context,
    port: Int,
    private val token: String,
    private val sensors: SensorHub,
    private val location: LocationHub,
    private val tts: TtsEngine,
    private val stt: SttEngine,
    private val whisper: WhisperEngine,
    private val camera: CameraHub,
    private val dashcam: Dashcam,
    private val files: FileShare,
    private val detector: Detector,
) : NanoHTTPD(port) {

    override fun serve(session: IHTTPSession): Response = try {
        if (!authorized(session)) {
            json(Response.Status.UNAUTHORIZED, JSONObject().put("error", "missing or bad token"))
        } else {
            route(session)
        }
    } catch (e: CameraBusy) {
        json(Response.Status.CONFLICT, JSONObject().put("error", e.message))
    } catch (e: Exception) {
        json(Response.Status.INTERNAL_ERROR, JSONObject().put("error", e.toString()))
    }

    // SSE must not be buffered by gzip.
    override fun useGzipWhenAccepted(r: Response) = false

    private fun route(s: IHTTPSession): Response {
        if (files.handles(s.uri)) return files.serve(s)
        val path = s.uri.trimEnd('/').ifEmpty { "/" }
        return when {
            s.method == Method.GET && path == "/" -> json(info())
            s.method == Method.GET && path == "/status" -> json(DeviceStatus.snapshot(context))
            s.method == Method.GET && path == "/sensors" -> json(sensors.snapshot())
            s.method == Method.GET && path == "/location" -> json(location.snapshot())
            s.method == Method.GET && path == "/all" -> json(all())
            s.method == Method.GET && path == "/stream" -> stream(s)
            s.method == Method.POST && path == "/tts" -> tts(s)
            s.method == Method.POST && path == "/stt" -> stt(s)
            s.method == Method.POST && path == "/transcribe" -> transcribe(s)
            s.method == Method.GET && path == "/models" -> json(models())
            s.method == Method.GET && path == "/camera/list" ->
                json(JSONObject().put("cameras", camera.list()).put("max_open", camera.maxOpen))
            s.method == Method.POST && path == "/camera/max_open" ->
                json(JSONObject().put("max_open", (s.param("n")?.toIntOrNull() ?: 1).coerceIn(1, 3).also { camera.maxOpen = it }))
            s.method == Method.GET && path == "/camera/snapshot" -> snapshot(s)
            s.method == Method.GET && path == "/camera/mjpeg" -> mjpeg(s)
            s.method == Method.GET && path == "/dashcam" -> json(dashcam.status())
            s.method == Method.POST && path == "/dashcam/start" -> json(dashcam.heartbeat(
                (s.param("cams") ?: s.param("cam"))?.split(",")?.map { it.trim() }?.filter { it.isNotEmpty() },
                s.param("keepalive_s")?.toIntOrNull(),
                s.param("bitrate")?.toIntOrNull()))
            s.method == Method.POST && path == "/dashcam/stop" -> json(dashcam.stop())
            s.method == Method.POST && path == "/dashcam/lock" -> json(dashcam.lockNow(s.param("reason") ?: "manual"))
            s.method == Method.GET && path == "/detect" -> json(detector.status())
            s.method == Method.POST && path == "/detect/start" -> json(detector.start(Detector.Config(
                s.param("cam"),
                s.param("fps")?.toIntOrNull()?.coerceIn(1, 15) ?: 5,
                s.param("model") ?: Detector.DEFAULT_MODEL,
                detectSize(s),
                detectThresh(s))))
            s.method == Method.POST && path == "/detect/stop" -> json(detector.stop())
            s.method == Method.GET && path == "/detect/stream" -> detectStream()
            s.method == Method.POST && path == "/detect/once" ->
                json(detector.once(s.param("cam"), s.param("model") ?: Detector.DEFAULT_MODEL, detectSize(s), detectThresh(s)))
            s.method == Method.POST && path == "/detect/image" ->
                json(detector.image(readBytes(s, 32 shl 20), s.param("model") ?: Detector.DEFAULT_MODEL, detectSize(s), detectThresh(s)))
            else -> json(Response.Status.NOT_FOUND, JSONObject().put("error", "no route ${s.method} $path"))
        }
    }

    private fun info() = JSONObject()
        .put("name", "DashLink")
        .put("version", BuildConfigInfo.version(context))
        .put("sensors", JSONArray(sensors.available))
        .put("tts_ready", tts.ready)
        .put("tts_engine", tts.engine ?: JSONObject.NULL)
        .put("stt_available", stt.available)
        .put("endpoints", JSONArray(listOf(
            "GET /status", "GET /sensors", "GET /location", "GET /all",
            "GET /stream?hz=10", "POST /tts {text, play, rate, pitch}",
            "POST /stt?lang=en-US&offline=1&max_ms=8000",
            "POST /transcribe?model=base.en&lang=en&prompt=... (body: WAV)", "GET /models",
            "GET /camera/list", "GET /camera/snapshot?cam=", "GET /camera/mjpeg?cam=&fps=",
            "GET /dashcam", "POST /dashcam/start?cams=2,1&keepalive_s=30 (heartbeat)", "POST /dashcam/stop",
            "POST /dashcam/lock", "WebDAV /files/ (models, dashcam, detector, share)",
            "POST /detect/start?cam=2&fps=5&model=yolo11n&size=640&thresh=0.35", "POST /detect/stop",
            "GET /detect", "GET /detect/stream", "POST /detect/once?cam=", "POST /detect/image (body: JPEG/PNG)",
        )))

    private fun all() = JSONObject()
        .put("t", System.currentTimeMillis())
        .put("sensors", sensors.snapshot())
        .put("location", location.snapshot())

    private fun stream(s: IHTTPSession): Response {
        val hz = s.param("hz")?.toDoubleOrNull()?.coerceIn(0.2, 50.0) ?: 10.0
        val r = newChunkedResponse(Response.Status.OK, "text/event-stream",
            SseStream((1000 / hz).toLong()) { all().toString() })
        r.addHeader("Cache-Control", "no-cache")
        return r
    }

    private fun tts(s: IHTTPSession): Response {
        val body = readBody(s)
        val obj = body.trim().let { if (it.startsWith("{")) JSONObject(it) else JSONObject() }
        val text = obj.optString("text").ifEmpty { s.param("text") ?: body.takeUnless { it.trim().startsWith("{") } ?: "" }.trim()
        if (text.isEmpty()) return json(Response.Status.BAD_REQUEST, JSONObject().put("error", "no text"))
        val rate = obj.optDouble("rate", s.param("rate")?.toDoubleOrNull() ?: 1.0).toFloat()
        val pitch = obj.optDouble("pitch", s.param("pitch")?.toDoubleOrNull() ?: 1.0).toFloat()
        val play = obj.optBoolean("play", s.param("play") in setOf("1", "true"))

        if (play) {
            val ok = tts.speak(text, rate, pitch)
            return json(if (ok) Response.Status.OK else Response.Status.SERVICE_UNAVAILABLE, JSONObject().put("ok", ok))
        }
        val wav = tts.synthesize(text, rate, pitch)
            ?: return json(Response.Status.SERVICE_UNAVAILABLE, JSONObject().put("ok", false).put("error", "tts failed"))
        return newFixedLengthResponse(Response.Status.OK, "audio/wav", DeletingFileStream(wav), wav.length())
    }

    private fun stt(s: IHTTPSession): Response {
        val lang = s.param("lang") ?: "en-US"
        val offline = s.param("offline") !in setOf("0", "false")
        val maxMs = s.param("max_ms")?.toLongOrNull()?.coerceIn(1000, 60_000) ?: 8000
        // "No speech" is a normal outcome, so this is 200 either way; check "ok".
        return json(stt.listen(lang, offline, maxMs))
    }

    private fun transcribe(s: IHTTPSession): Response {
        val audio = readBytes(s, 32 shl 20)
        if (audio.isEmpty()) return json(Response.Status.BAD_REQUEST, JSONObject().put("error", "empty body; POST a WAV"))
        val result = whisper.transcribe(audio, s.param("model"), s.param("lang") ?: "en", s.param("prompt"))
        return json(if (result.optBoolean("ok")) Response.Status.OK else Response.Status.SERVICE_UNAVAILABLE, result)
    }

    private fun models() = JSONObject()
        .put("dir", whisper.modelDir.path)
        .put("installed", JSONArray(whisper.models()))
        .put("default", whisper.defaultModel() ?: JSONObject.NULL)

    private fun detectSize(s: IHTTPSession) = s.param("size")?.toIntOrNull()?.coerceIn(160, 1280)?.let { it / 32 * 32 } ?: 640

    private fun detectThresh(s: IHTTPSession) = s.param("thresh")?.toFloatOrNull()?.coerceIn(0.05f, 0.95f) ?: 0.35f

    /** One SSE frame per new detection result. */
    private fun detectStream(): Response {
        var last = 0L
        val r = newChunkedResponse(Response.Status.OK, "text/event-stream", SseStream(0) {
            val next = detector.awaitNext(last, 15_000)
            if (next != null) {
                last = next.optLong("seq")
                next.toString()
            } else {
                JSONObject().put("idle", true).toString() // keeps the connection alive while stopped
            }
        })
        r.addHeader("Cache-Control", "no-cache")
        return r
    }

    private fun snapshot(s: IHTTPSession): Response {
        val jpeg = camera.snapshot(s.param("cam"))
            ?: return json(Response.Status.SERVICE_UNAVAILABLE, JSONObject().put("error", "no frame from camera"))
        return newFixedLengthResponse(Response.Status.OK, "image/jpeg", jpeg.inputStream(), jpeg.size.toLong())
    }

    private fun mjpeg(s: IHTTPSession): Response {
        val fps = s.param("fps")?.toIntOrNull()?.coerceIn(1, 30) ?: 10
        val unit = camera.acquireViewer(s.param("cam"))
        unit.maxFps = fps
        val r = newChunkedResponse(Response.Status.OK, "multipart/x-mixed-replace; boundary=$BOUNDARY", MjpegStream(unit))
        r.addHeader("Cache-Control", "no-cache")
        return r
    }

    /** Loopback (adb forward, local apps) is trusted; anything over the network needs the token. */
    private fun authorized(s: IHTTPSession): Boolean {
        val ip = s.remoteIpAddress ?: ""
        if (ip == "127.0.0.1" || ip == "::1" || ip == "0:0:0:0:0:0:0:1") return true
        return s.headers["authorization"] == "Bearer $token" || s.param("token") == token
    }

    private fun IHTTPSession.param(name: String): String? = parameters[name]?.firstOrNull()

    // Read the raw body ourselves: parseBody() swallows form-encoded bodies (curl -d's default).
    private fun readBody(s: IHTTPSession): String = String(readBytes(s, 1 shl 20))

    private fun readBytes(s: IHTTPSession, cap: Int): ByteArray {
        val len = s.headers["content-length"]?.toIntOrNull()?.coerceIn(0, cap) ?: 0
        val buf = ByteArray(len)
        var read = 0
        while (read < len) {
            val n = s.inputStream.read(buf, read, len - read)
            if (n < 0) break
            read += n
        }
        return if (read == len) buf else buf.copyOf(read)
    }

    private fun json(obj: JSONObject) = json(Response.Status.OK, obj)

    private fun json(status: Response.Status, obj: JSONObject): Response =
        newFixedLengthResponse(status, "application/json", obj.toString())
}

/** Server-sent events: produces one `data:` frame per interval until the client goes away. */
private class SseStream(private val intervalMs: Long, private val next: () -> String) : InputStream() {
    private var buf = ByteArray(0)
    private var pos = 0
    private var first = true

    private fun fill() {
        if (!first) Thread.sleep(intervalMs)
        first = false
        buf = "data: ${next()}\n\n".toByteArray()
        pos = 0
    }

    override fun read(): Int {
        if (pos >= buf.size) fill()
        return buf[pos++].toInt() and 0xff
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
        if (len == 0) return 0
        if (pos >= buf.size) fill()
        val n = minOf(len, buf.size - pos)
        System.arraycopy(buf, pos, b, off, n)
        pos += n
        return n
    }
}

private const val BOUNDARY = "dashlinkframe"

/** multipart/x-mixed-replace JPEG parts; releases the camera when the client goes away. */
private class MjpegStream(private val camera: CameraUnit) : InputStream() {
    private var buf = ByteArray(0)
    private var pos = 0
    private var seq = camera.latestSeq() // never replay a frame from an earlier session
    private var closed = false

    private fun fill() {
        val f = camera.nextFrame(seq, 10_000) ?: throw java.io.IOException("camera stopped producing frames")
        seq = f.seq
        val head = "--$BOUNDARY\r\nContent-Type: image/jpeg\r\nContent-Length: ${f.jpeg.size}\r\n\r\n".toByteArray()
        buf = head + f.jpeg + "\r\n".toByteArray()
        pos = 0
    }

    override fun read(): Int {
        if (pos >= buf.size) fill()
        return buf[pos++].toInt() and 0xff
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
        if (len == 0) return 0
        if (pos >= buf.size) fill()
        val n = minOf(len, buf.size - pos)
        System.arraycopy(buf, pos, b, off, n)
        pos += n
        return n
    }

    override fun close() {
        if (!closed) {
            closed = true
            camera.releaseViewer()
        }
    }
}

private class DeletingFileStream(private val file: File) : FileInputStream(file) {
    override fun close() {
        super.close()
        file.delete()
    }
}

object BuildConfigInfo {
    fun version(context: Context): String =
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
}
