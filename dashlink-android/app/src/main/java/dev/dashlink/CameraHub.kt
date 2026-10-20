package dev.dashlink

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import android.view.Surface
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class CameraBusy(message: String) : Exception(message)

class Frame(val seq: Long, val time: Long, val jpeg: ByteArray)

/** Uncompressed frame for on-device analysis (object detection), NV21 layout. */
class RawFrame(val seq: Long, val time: Long, val width: Int, val height: Int, val nv21: ByteArray)

/**
 * Every camera the phone exposes, each run independently by a [CameraUnit].
 * Whether two can actually stream at once is up to the hardware; see [maxOpen].
 */
class CameraHub(context: Context) {

    private val cm = context.getSystemService(CameraManager::class.java)
    private val units = java.util.concurrent.ConcurrentHashMap<String, CameraUnit>()

    /** How many cameras may be open together. Opening past this is refused rather than letting
     *  Android evict a camera that's already in use (which would kill a recording). */
    @Volatile var maxOpen = 2 // measured on the G7: two stream fine, a third fails to configure

    /** Called with the camera id when a camera is taken away or errors out. */
    var onLost: ((String) -> Unit)? = null

    fun unit(id: String): CameraUnit {
        require(id in cm.cameraIdList) { "no camera $id" }
        return units.getOrPut(id) { CameraUnit(cm, id) { onLost?.invoke(id) } }
    }

    fun openIds(): List<String> = units.values.filter { it.isOpen }.map { it.id }

    /** Throws [CameraBusy] if opening [id] would exceed [maxOpen]. */
    private fun admit(id: String) {
        val others = openIds().filter { it != id }
        if (others.size >= maxOpen) {
            throw CameraBusy("camera ${others.joinToString()} in use; this phone allows $maxOpen open at once")
        }
    }

    /** Explicit id, else an already-open camera, else the main rear camera. */
    fun resolve(id: String?): String = id ?: openIds().firstOrNull() ?: defaultId()

    fun acquireViewer(id: String?): CameraUnit {
        val u = unit(resolve(id))
        if (!u.isOpen) admit(u.id)
        u.acquireViewer()
        return u
    }

    /** Like [acquireViewer], for consumers of raw NV21 frames; pair with [CameraUnit.releaseRaw]. */
    fun acquireRaw(id: String?): CameraUnit {
        val u = unit(resolve(id))
        if (!u.isOpen) admit(u.id)
        u.acquireRaw()
        return u
    }

    fun snapshot(id: String?, fps: Int? = null): ByteArray? {
        val u = acquireViewer(id)
        try {
            fps?.let { u.maxFps = it }
            return u.snapshot()
        } finally {
            u.releaseViewer()
        }
    }

    fun setRecorder(id: String, surface: Surface?): Boolean {
        val u = unit(id)
        if (surface != null && !u.isOpen) admit(id)
        return u.setRecorder(surface)
    }

    fun recordSize(id: String): Size = unit(id).recordSize()

    fun shutdown() = units.values.forEach { it.shutdown() }

    fun list(): JSONArray = JSONArray().also { out ->
        for (id in cm.cameraIdList) {
            val c = cm.getCameraCharacteristics(id)
            out.put(JSONObject()
                .put("id", id)
                .put("facing", when (c.get(CameraCharacteristics.LENS_FACING)) {
                    CameraCharacteristics.LENS_FACING_BACK -> "back"
                    CameraCharacteristics.LENS_FACING_FRONT -> "front"
                    else -> "external"
                })
                .put("focal_mm", c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.firstOrNull()?.toDouble())
                .put("sensor_orientation", c.get(CameraCharacteristics.SENSOR_ORIENTATION))
                .put("record_size", unit(id).recordSize().toString())
                .put("open", units[id]?.isOpen == true)
                .put("recording", units[id]?.isRecording == true))
        }
    }

    private fun backIds() = cm.cameraIdList.filter {
        cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
    }

    private fun focal(id: String) =
        cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.firstOrNull() ?: Float.MAX_VALUE

    fun defaultId(): String = backIds().firstOrNull() ?: cm.cameraIdList.first()

    /** Widest rear lens (shortest focal length): best field of view for a dashcam. */
    fun widestBackId(): String = backIds().minByOrNull { focal(it) } ?: defaultId()

    fun frontId(): String? = cm.cameraIdList.firstOrNull {
        cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
    }
}

/** One camera, shared by live viewers (JPEG frames) and a recorder. State lives on [handler]'s thread. */
class CameraUnit(private val cm: CameraManager, val id: String, private val onLost: () -> Unit) {

    private val handler = Handler(HandlerThread("camera-$id").apply { start() }.looper)
    private val frameHandler = Handler(HandlerThread("camera-$id-frames").apply { start() }.looper)

    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var recorderSurface: Surface? = null

    @Volatile var isOpen = false
        private set
    @Volatile var isRecording = false
        private set
    @Volatile var openedAt = 0L
        private set

    private val viewers = AtomicInteger(0)
    private val rawViewers = AtomicInteger(0)
    private fun consumers() = viewers.get() + rawViewers.get()
    private val closeIfIdle = Runnable { if (consumers() == 0 && recorderSurface == null) closeCamera() }

    private val frameLock = Object()
    private var latest: Frame? = null
    @Volatile private var lastEncode = 0L
    @Volatile var maxFps = 15

    private val rawLock = Object()
    private var latestRaw: RawFrame? = null
    @Volatile private var lastRaw = 0L
    @Volatile var rawMaxFps = 10

    fun recordSize(): Size = pickSize(MediaRecorder::class.java, 1920)

    private fun previewSize(): Size = pickSize(ImageReader::class.java, 1280, ImageFormat.YUV_420_888)

    private fun pickSize(klass: Class<*>, maxWidth: Int, format: Int? = null): Size {
        val map = cm.getCameraCharacteristics(id).get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!
        val sizes = if (format != null) map.getOutputSizes(format) else map.getOutputSizes(klass)
        val wide = sizes.filter { it.width * 9 == it.height * 16 && it.width <= maxWidth }
        return wide.maxByOrNull { it.width } ?: sizes.minByOrNull { Math.abs(it.width - maxWidth) }!!
    }

    // ---- viewers -------------------------------------------------------------------------

    /** Register a frame consumer; pair with [releaseViewer]. */
    fun acquireViewer() {
        viewers.incrementAndGet()
        isOpen = true // reserve now so the hub's open-count check sees it
        handler.post {
            handler.removeCallbacks(closeIfIdle)
            if (device == null) configure(null) else updateRequest()
        }
    }

    fun releaseViewer() {
        if (viewers.decrementAndGet() == 0) consumerGone()
    }

    fun acquireRaw() {
        rawViewers.incrementAndGet()
        isOpen = true
        handler.post {
            handler.removeCallbacks(closeIfIdle)
            if (device == null) configure(null) else updateRequest()
        }
    }

    fun releaseRaw() {
        if (rawViewers.decrementAndGet() == 0) consumerGone()
    }

    private fun consumerGone() {
        handler.post {
            updateRequest()
            handler.postDelayed(closeIfIdle, 5000)
        }
    }

    /** Next raw frame newer than [afterSeq], or null on timeout. */
    fun nextRaw(afterSeq: Long, timeoutMs: Long): RawFrame? {
        val deadline = System.currentTimeMillis() + timeoutMs
        synchronized(rawLock) {
            while (true) {
                latestRaw?.let { if (it.seq > afterSeq) return it }
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) return null
                rawLock.wait(left)
            }
        }
    }

    /** Next frame newer than [afterSeq], or null on timeout. */
    fun nextFrame(afterSeq: Long, timeoutMs: Long): Frame? {
        val deadline = System.currentTimeMillis() + timeoutMs
        synchronized(frameLock) {
            while (true) {
                latest?.let { if (it.seq > afterSeq) return it }
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) return null
                frameLock.wait(left)
            }
        }
    }

    fun latestSeq(): Long = synchronized(frameLock) { latest?.seq ?: 0 }

    /** One JPEG, skipping the first frames after opening while exposure settles. Caller holds a viewer. */
    fun snapshot(timeoutMs: Long = 8000): ByteArray? {
        var seq = latestSeq()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val f = nextFrame(seq, deadline - System.currentTimeMillis()) ?: return null
            if (f.time - openedAt >= 800) return f.jpeg
            seq = f.seq
        }
        return null
    }

    // ---- recorder ------------------------------------------------------------------------

    /** Attach (surface != null) or detach the recorder. Blocks until the session is reconfigured. */
    fun setRecorder(surface: Surface?): Boolean {
        val done = CompletableFuture<Boolean>()
        if (surface != null) isOpen = true
        handler.post {
            recorderSurface = surface
            isRecording = surface != null
            when {
                surface != null -> configure(done)
                consumers() == 0 -> { closeCamera(); done.complete(true) }
                else -> createSession(done)
            }
        }
        return try {
            done.get(10, TimeUnit.SECONDS)
        } catch (e: Exception) {
            Log.e(TAG, "camera reconfigure timed out", e)
            false
        }
    }

    fun shutdown() {
        handler.post { recorderSurface = null; closeCamera() }
    }

    // ---- camera thread -------------------------------------------------------------------

    @SuppressLint("MissingPermission")
    private fun configure(done: CompletableFuture<Boolean>?) {
        if (device != null) {
            createSession(done)
            return
        }
        try {
            cm.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    device = camera
                    openedAt = System.currentTimeMillis()
                    val size = previewSize()
                    reader = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 3).apply {
                        setOnImageAvailableListener({ onImage(it) }, frameHandler)
                    }
                    createSession(done)
                }

                override fun onDisconnected(camera: CameraDevice) = lost(camera, "disconnected")
                override fun onError(camera: CameraDevice, error: Int) = lost(camera, "error $error")

                private fun lost(camera: CameraDevice, why: String) {
                    Log.w(TAG, "camera $id $why")
                    if (device == camera || device == null) closeCamera()
                    camera.close()
                    done?.complete(false)
                    onLost()
                }
            }, handler)
        } catch (e: Exception) {
            Log.e(TAG, "openCamera($id) failed", e)
            isOpen = false
            done?.complete(false)
        }
    }

    @Suppress("DEPRECATION")
    private fun createSession(done: CompletableFuture<Boolean>?) {
        val d = device ?: run { done?.complete(false); return }
        session?.close()
        session = null
        val surfaces = listOfNotNull(reader?.surface, recorderSurface)
        try {
            d.createCaptureSession(surfaces, object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) {
                    session = s
                    updateRequest()
                    done?.complete(true)
                }

                override fun onConfigureFailed(s: CameraCaptureSession) {
                    Log.e(TAG, "session configure failed")
                    done?.complete(false)
                }
            }, handler)
        } catch (e: Exception) {
            Log.e(TAG, "createCaptureSession failed", e)
            done?.complete(false)
        }
    }

    /** Point the repeating request at whoever currently wants frames. */
    private fun updateRequest() {
        val s = session ?: return
        val d = device ?: return
        val targets = listOfNotNull(recorderSurface, reader?.surface?.takeIf { consumers() > 0 })
        try {
            if (targets.isEmpty()) {
                s.stopRepeating()
                return
            }
            val template = if (recorderSurface != null) CameraDevice.TEMPLATE_RECORD else CameraDevice.TEMPLATE_PREVIEW
            val req = d.createCaptureRequest(template).apply {
                targets.forEach { addTarget(it) }
                set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            }
            s.setRepeatingRequest(req.build(), null, handler)
        } catch (e: Exception) {
            Log.e(TAG, "setRepeatingRequest failed", e)
        }
    }

    private fun closeCamera() {
        runCatching { session?.close() }
        runCatching { device?.close() }
        runCatching { reader?.close() }
        session = null
        device = null
        reader = null
        isOpen = consumers() > 0 || recorderSurface != null
    }

    // ---- frames (frame thread) -----------------------------------------------------------

    private fun onImage(r: ImageReader) {
        val img = runCatching { r.acquireLatestImage() }.getOrNull() ?: return
        try {
            val now = System.currentTimeMillis()
            val wantJpeg = viewers.get() > 0 && now - lastEncode >= 1000L / maxFps
            val wantRaw = rawViewers.get() > 0 && now - lastRaw >= 1000L / rawMaxFps
            if (!wantJpeg && !wantRaw) return
            val nv21 = toNv21(img)
            if (wantRaw) {
                lastRaw = now
                synchronized(rawLock) {
                    latestRaw = RawFrame((latestRaw?.seq ?: 0) + 1, now, img.width, img.height, nv21)
                    rawLock.notifyAll()
                }
            }
            if (wantJpeg) {
                lastEncode = now
                val jpeg = jpegFromNv21(nv21, img.width, img.height, 80)
                synchronized(frameLock) {
                    latest = Frame((latest?.seq ?: 0) + 1, now, jpeg)
                    frameLock.notifyAll()
                }
            }
        } finally {
            img.close()
        }
    }

    private fun jpegFromNv21(nv21: ByteArray, w: Int, h: Int, quality: Int): ByteArray {
        val out = ByteArrayOutputStream(w * h / 4)
        YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(Rect(0, 0, w, h), quality, out)
        return out.toByteArray()
    }

    private fun toNv21(image: Image): ByteArray {
        val w = image.width
        val h = image.height
        val nv21 = ByteArray(w * h * 3 / 2)
        var pos = 0

        val y = image.planes[0]
        val yb = y.buffer
        for (row in 0 until h) {
            yb.position(row * y.rowStride)
            yb.get(nv21, pos, w)
            pos += w
        }

        val u = image.planes[1]
        val v = image.planes[2]
        val ub = u.buffer
        val vb = v.buffer
        if (v.pixelStride == 2 && u.pixelStride == 2 && v.rowStride == u.rowStride) {
            // Semi-planar (Qualcomm): the V plane's bytes already run V U V U..., which is NV21.
            for (row in 0 until h / 2) {
                val start = row * v.rowStride
                val n = minOf(w, vb.capacity() - start)
                vb.position(start)
                vb.get(nv21, pos, n)
                if (n < w) nv21[pos + w - 1] = ub.get(start + w - 2)
                pos += w
            }
        } else {
            for (row in 0 until h / 2) {
                for (col in 0 until w / 2) {
                    nv21[pos++] = vb.get(row * v.rowStride + col * v.pixelStride)
                    nv21[pos++] = ub.get(row * u.rowStride + col * u.pixelStride)
                }
            }
        }
        return nv21
    }

    companion object {
        private const val TAG = "DashLinkCamera"
    }
}
