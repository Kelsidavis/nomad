package dev.dashlink

import android.content.Context
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

/**
 * Loop recorder that runs only while the Pi keeps sending heartbeats (i.e. while the car is on).
 * Records one or more cameras at once (the G7 manages two), each in one-minute H.264 segments
 * with a GPS/motion CSV beside each. Oldest unlocked segments are deleted first.
 * All state lives on [handler]'s thread.
 */
class Dashcam(
    context: Context,
    private val camera: CameraHub,
    private val sensors: SensorHub,
    private val location: LocationHub,
) {
    val dir: File = context.getExternalFilesDir("dashcam")!!.also { it.mkdirs() }
    private val handler = Handler(HandlerThread("dashcam").apply { start() }.looper)

    var segmentSeconds = 60
    var maxBytes = 30L shl 30
    var minFreeBytes = 3L shl 30
    var rotation = 0

    private val channels = linkedMapOf<String, Channel>()
    private var lastBeat = 0L
    private var keepaliveMs = 30_000L
    private var lastStop: String? = null
    private var lastLock: String? = null

    private val watchdog = object : Runnable {
        override fun run() {
            if (channels.isNotEmpty() && System.currentTimeMillis() - lastBeat > keepaliveMs) {
                stopAll("no heartbeat for ${keepaliveMs / 1000}s")
            }
            handler.postDelayed(this, 5000)
        }
    }

    private val logTick = object : Runnable {
        override fun run() {
            if (channels.isEmpty()) return
            val row = sidecarRow()
            channels.values.forEach { it.writeSidecar(row) }
            handler.postDelayed(this, 1000)
        }
    }

    init {
        handler.post(watchdog)
        camera.onLost = { id -> handler.post { channels[id]?.let { stopChannel(it, "camera $id lost") } } }
        sensors.onImpact = { g -> handler.post { lock("impact %.1fg".format(g)) } }
    }

    private fun <T> onThread(block: () -> T): T {
        val f = CompletableFuture<T>()
        handler.post {
            try {
                f.complete(block())
            } catch (e: Throwable) {
                f.completeExceptionally(e)
            }
        }
        return f.get(60, TimeUnit.SECONDS)
    }

    /**
     * "Car is on." Makes the set of recording cameras match [cams] (default: widest rear lens)
     * and keeps them going; everything stops when these stop arriving.
     */
    fun heartbeat(cams: List<String>?, keepaliveS: Int?, bitrate: Int?): JSONObject = onThread {
        lastBeat = System.currentTimeMillis()
        keepaliveS?.let { keepaliveMs = it.coerceIn(10, 600) * 1000L }
        val want = cams?.takeIf { it.isNotEmpty() } ?: channels.keys.toList().ifEmpty { listOf(camera.widestBackId()) }
        for (id in channels.keys.toList() - want.toSet()) stopChannel(channels.getValue(id), "camera $id deselected")
        for (id in want) {
            if (id !in channels) startChannel(id, bitrate ?: if (id == camera.frontId()) 4_000_000 else 8_000_000)
        }
        if (channels.isNotEmpty()) {
            handler.removeCallbacks(logTick)
            handler.post(logTick)
        }
        statusJson()
    }

    fun stop(reason: String = "stop requested"): JSONObject = onThread {
        stopAll(reason)
        statusJson()
    }

    fun lockNow(reason: String = "manual"): JSONObject = onThread {
        lock(reason)
        statusJson()
    }

    fun status(): JSONObject = onThread { statusJson() }

    fun shutdown() = onThread { stopAll("service stopping") }

    // ---- dashcam thread ------------------------------------------------------------------

    private inner class Channel(val camId: String, val recorder: MediaRecorder, var current: File, val bitrate: Int) {
        var previous: File? = null
        var pendingNext: File? = null
        private var sidecar: FileWriter? = null

        fun openSidecar() {
            closeSidecar()
            sidecar = runCatching {
                FileWriter(sidecarFor(current)).apply { write(SIDECAR_HEADER) }
            }.getOrNull()
        }

        fun writeSidecar(row: String) {
            runCatching {
                sidecar?.write(row)
                sidecar?.flush()
            }
        }

        fun closeSidecar() {
            runCatching { sidecar?.close() }
            sidecar = null
        }
    }

    private fun startChannel(id: String, bitrate: Int) {
        val size = runCatching { camera.recordSize(id) }.getOrElse {
            lastStop = "camera $id: ${it.message}"
            return
        }
        val file = newSegmentFile(id)
        val r = MediaRecorder()
        try {
            r.setVideoSource(MediaRecorder.VideoSource.SURFACE)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setVideoEncoder(MediaRecorder.VideoEncoder.H264)
            r.setVideoEncodingBitRate(bitrate)
            r.setVideoFrameRate(30)
            r.setVideoSize(size.width, size.height)
            r.setOrientationHint(rotation)
            // Segments roll over by size, which MediaRecorder can do seamlessly via setNextOutputFile.
            r.setMaxFileSize(bitrate / 8L * segmentSeconds)
            r.setOutputFile(file)
            r.setOnInfoListener { _, what, _ -> onRecorderInfo(id, what) }
            r.prepare()
        } catch (e: Exception) {
            Log.e(TAG, "recorder setup failed for camera $id", e)
            r.release()
            file.delete()
            lastStop = "camera $id recorder setup failed: $e"
            return
        }
        val attached = try {
            camera.setRecorder(id, r.surface)
        } catch (e: CameraBusy) {
            lastStop = "camera $id: ${e.message}"
            false
        }
        if (!attached) {
            r.release()
            file.delete()
            if (lastStop == null || !lastStop!!.startsWith("camera $id")) lastStop = "camera $id unavailable"
            return
        }
        try {
            r.start()
        } catch (e: Exception) {
            Log.e(TAG, "recorder start failed for camera $id", e)
            camera.setRecorder(id, null)
            r.release()
            file.delete()
            lastStop = "camera $id recorder start failed: $e"
            return
        }
        val ch = Channel(id, r, file, bitrate)
        channels[id] = ch
        ch.openSidecar()
        lastStop = null
        enforceRetention()
        Log.i(TAG, "recording camera $id at $size, ${bitrate / 1_000_000} Mbps, to ${file.name}")
    }

    private fun onRecorderInfo(id: String, what: Int) {
        val ch = channels[id] ?: return
        when (what) {
            MediaRecorder.MEDIA_RECORDER_INFO_MAX_FILESIZE_APPROACHING -> {
                val next = newSegmentFile(id)
                try {
                    ch.recorder.setNextOutputFile(next)
                    ch.pendingNext = next
                } catch (e: Exception) {
                    Log.e(TAG, "setNextOutputFile failed", e)
                }
            }
            MediaRecorder.MEDIA_RECORDER_INFO_NEXT_OUTPUT_FILE_STARTED -> {
                val next = ch.pendingNext ?: return
                ch.previous = ch.current
                ch.current = next
                ch.pendingNext = null
                ch.openSidecar()
                enforceRetention()
            }
            MediaRecorder.MEDIA_RECORDER_INFO_MAX_FILESIZE_REACHED -> {
                // Only happens if the rollover above failed; restart this channel cleanly.
                stopChannel(ch, "segment rollover failed")
                startChannel(id, ch.bitrate)
            }
        }
    }

    private fun stopChannel(ch: Channel, reason: String) {
        channels.remove(ch.camId)
        camera.setRecorder(ch.camId, null)
        try {
            ch.recorder.stop()
        } catch (e: RuntimeException) {
            // No frames reached the file; it's unplayable.
            ch.current.delete()
            sidecarFor(ch.current).delete()
        }
        ch.recorder.release()
        ch.pendingNext?.takeIf { it.length() == 0L }?.delete()
        ch.closeSidecar()
        lastStop = reason
        Log.i(TAG, "camera ${ch.camId} stopped: $reason")
    }

    private fun stopAll(reason: String) {
        channels.values.toList().forEach { stopChannel(it, reason) }
        handler.removeCallbacks(logTick)
    }

    /** Protect every channel's current and previous segments from deletion (incident). */
    private fun lock(reason: String) {
        fun locked(f: File?): File? {
            if (f == null || !f.exists() || f.name.contains(LOCKED)) return f
            val to = File(f.parentFile, f.name.replace(".mp4", "$LOCKED.mp4"))
            // Renaming an open file is fine on Linux; the recorder keeps writing via its fd.
            sidecarFor(f).renameTo(sidecarFor(to))
            return if (f.renameTo(to)) to else f
        }
        for (ch in channels.values) {
            ch.previous = locked(ch.previous)
            ch.current = locked(ch.current)!!
        }
        lastLock = "$reason at ${stamp(Date())}"
        Log.i(TAG, "locked: $reason")
    }

    private fun enforceRetention() {
        val keep = channels.values.flatMap { listOfNotNull(it.current, it.previous, it.pendingNext) }.map { it.name }.toSet()
        val candidates = segments().filter { !it.name.contains(LOCKED) && it.name !in keep }.sortedBy { it.name }.toMutableList()
        var total = dirBytes()
        while (candidates.isNotEmpty() && (total > maxBytes || dir.usableSpace < minFreeBytes)) {
            val f = candidates.removeAt(0)
            total -= f.length() + sidecarFor(f).length()
            sidecarFor(f).delete()
            f.delete()
        }
    }

    private fun segments() = dir.listFiles { f -> f.name.endsWith(".mp4") }?.toList().orEmpty()

    private fun dirBytes() = dir.listFiles()?.sumOf { it.length() } ?: 0L

    private fun newSegmentFile(camId: String): File {
        val base = "${stamp(Date())}_cam$camId"
        var f = File(dir, "$base.mp4")
        var n = 1
        while (f.exists()) f = File(dir, "${base}_${n++}.mp4")
        return f
    }

    private fun sidecarFor(video: File) = File(video.parentFile, video.name.removeSuffix(".mp4") + ".csv")

    private fun sidecarRow(): String {
        val loc = location.snapshot()
        val s = sensors.snapshot()
        val accel = s.optJSONObject("linear_accel")?.optJSONArray("values")
        fun v(o: JSONObject?, k: String) = o?.opt(k)?.takeIf { it != JSONObject.NULL }?.toString() ?: ""
        return listOf(
            iso.format(Date()),
            v(loc, "lat"), v(loc, "lon"), v(loc, "speed_mps"), v(loc, "bearing_deg"), v(loc, "accuracy_m"),
            v(s.optJSONObject("orientation"), "heading_deg"),
            accel?.optDouble(0)?.toString() ?: "", accel?.optDouble(1)?.toString() ?: "", accel?.optDouble(2)?.toString() ?: "",
        ).joinToString(",") + "\n"
    }

    private fun statusJson(): JSONObject {
        val segs = segments()
        val chans = JSONArray()
        for (ch in channels.values) {
            chans.put(JSONObject().put("camera", ch.camId).put("segment", ch.current.name))
        }
        return JSONObject()
            .put("recording", channels.isNotEmpty())
            .put("channels", chans)
            .put("segments", segs.size)
            .put("locked", segs.count { it.name.contains(LOCKED) })
            .put("bytes", dirBytes())
            .put("free_bytes", dir.usableSpace)
            .put("max_bytes", maxBytes)
            .put("heartbeat_age_ms", if (lastBeat == 0L) JSONObject.NULL else System.currentTimeMillis() - lastBeat)
            .put("keepalive_s", keepaliveMs / 1000)
            .put("last_stop", lastStop ?: JSONObject.NULL)
            .put("last_lock", lastLock ?: JSONObject.NULL)
            .put("dir", dir.path)
    }

    private fun stamp(d: Date) = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US).format(d)

    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }

    companion object {
        private const val TAG = "DashLinkDashcam"
        private const val LOCKED = "-LOCKED"
        private const val SIDECAR_HEADER =
            "time_utc,lat,lon,speed_mps,bearing_deg,accuracy_m,heading_deg,accel_x,accel_y,accel_z\n"
    }
}
