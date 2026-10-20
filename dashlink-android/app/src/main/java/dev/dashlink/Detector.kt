package dev.dashlink

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer

/**
 * YOLO object detection (ncnn, CPU). Models are <name>.ncnn.param/.bin in
 * /sdcard/Android/data/dev.dashlink/files/detector/; yolo11n ships in the APK.
 * Measured on the G7: yolo11n at 640 px is ~66 ms per frame on 4 CPU threads.
 */
class DetectorEngine(context: Context) {

    val dir: File = context.getExternalFilesDir("detector")!!.also { it.mkdirs() }

    private var handle = 0L
    private var loaded: String? = null

    // Every native call runs on this one long-lived thread. The OpenMP runtime inside ncnn
    // crashed when driven from a fresh HTTP thread per request.
    private val worker = java.util.concurrent.Executors.newSingleThreadExecutor { r -> Thread(r, "detector-native") }

    private fun <T> onWorker(block: () -> T): T = try {
        worker.submit(java.util.concurrent.Callable { block() }).get()
    } catch (e: java.util.concurrent.ExecutionException) {
        throw e.cause ?: e
    }

    init {
        // Copy bundled models out of the APK once so they load like any other model file.
        for (name in context.assets.list("").orEmpty().filter { it.endsWith(".ncnn.param") || it.endsWith(".ncnn.bin") }) {
            val f = File(dir, name)
            if (!f.exists()) context.assets.open(name).use { input -> f.outputStream().use { input.copyTo(it) } }
        }
    }

    fun models(): List<String> =
        dir.listFiles { f -> f.name.endsWith(".ncnn.param") }
            ?.map { it.name.removeSuffix(".ncnn.param") }
            ?.filter { File(dir, "$it.ncnn.bin").isFile }
            ?.sorted()
            .orEmpty()

    fun detect(pixels: ByteArray, w: Int, h: Int, format: Int, model: String, size: Int, thresh: Float): JSONArray {
        val flat = onWorker {
            load(model)
            nativeDetect(handle, pixels, w, h, format, size, thresh)
        } ?: throw IllegalStateException("detect failed")
        val out = JSONArray()
        for (i in flat.indices step 6) {
            val label = flat[i + 4].toInt()
            out.put(JSONObject()
                .put("label", COCO.getOrElse(label) { "class$label" })
                .put("prob", Math.round(flat[i + 5] * 1000) / 1000.0)
                // Normalized 0..1, so callers don't need to know the frame size.
                .put("box", JSONArray(listOf(flat[i] / w, flat[i + 1] / h, flat[i + 2] / w, flat[i + 3] / h)
                    .map { Math.round(it * 10000) / 10000.0 })))
        }
        return out
    }

    private fun load(model: String) {
        if (loaded == model && handle != 0L) return
        val param = File(dir, "$model.ncnn.param")
        val bin = File(dir, "$model.ncnn.bin")
        require(param.isFile && bin.isFile) { "no detector model $model in ${dir.path}" }
        free()
        handle = nativeInit(param.path, bin.path, THREADS)
        check(handle != 0L) { "could not load detector model $model" }
        loaded = model
    }

    fun free() = onWorker {
        if (handle != 0L) nativeFree(handle)
        handle = 0L
        loaded = null
    }

    companion object {
        private const val THREADS = 4

        val COCO = listOf(
            "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
            "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
            "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
            "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
            "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
            "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
            "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
            "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
            "hair drier", "toothbrush",
        )

        init {
            System.loadLibrary("dashlink_detect")
        }

        @JvmStatic private external fun nativeInit(param: String, bin: String, threads: Int): Long
        @JvmStatic private external fun nativeFree(handle: Long)
        @JvmStatic private external fun nativeDetect(
            handle: Long, pixels: ByteArray, w: Int, h: Int, format: Int, target: Int, thresh: Float,
        ): FloatArray?
    }
}

/** Continuous detection on one camera's frames, with the newest result kept for HTTP clients. */
class Detector(private val camera: CameraHub, private val engine: DetectorEngine) {

    data class Config(val cam: String?, val fps: Int, val model: String, val size: Int, val thresh: Float)

    private val lock = Object()
    private var thread: Thread? = null
    @Volatile private var running = false
    private var unit: CameraUnit? = null
    private var config: Config? = null
    private var latest: JSONObject? = null
    private var seq = 0L
    private var lastError: String? = null

    fun start(c: Config): JSONObject {
        stop()
        val u = camera.acquireRaw(c.cam)
        u.rawMaxFps = c.fps
        synchronized(lock) {
            unit = u
            config = c.copy(cam = u.id)
            running = true
            lastError = null
        }
        thread = Thread({ loop(u, c) }, "detector").apply { start() }
        return status()
    }

    fun stop(): JSONObject {
        val t = thread
        running = false
        t?.interrupt()
        t?.join(2000)
        thread = null
        synchronized(lock) {
            unit?.releaseRaw()
            unit = null
        }
        return status()
    }

    private fun loop(u: CameraUnit, c: Config) {
        var frameSeq = 0L
        var throttled = false
        var lastThermalCheck = 0L
        while (running) {
            val f = try {
                u.nextRaw(frameSeq, 5000)
            } catch (e: InterruptedException) {
                break
            } ?: continue
            frameSeq = f.seq
            val now = System.currentTimeMillis()
            if (now - lastThermalCheck > 5000) {
                lastThermalCheck = now
                // Back off to 1 fps when the phone is hot; the dash in summer gets there quickly.
                val hot = (DeviceStatus.maxThermal() ?: 0.0) >= HOT_C
                if (hot != throttled) {
                    throttled = hot
                    u.rawMaxFps = if (hot) 1 else c.fps
                }
            }
            try {
                val t0 = System.currentTimeMillis()
                val objects = engine.detect(f.nv21, f.width, f.height, 0, c.model, c.size, c.thresh)
                val result = JSONObject()
                    .put("t", f.time)
                    .put("camera", u.id)
                    .put("frame", "${f.width}x${f.height}")
                    .put("infer_ms", System.currentTimeMillis() - t0)
                    .put("latency_ms", System.currentTimeMillis() - f.time)
                    .put("throttled", throttled)
                    .put("objects", objects)
                synchronized(lock) {
                    latest = result.put("seq", ++seq)
                    lock.notifyAll()
                }
            } catch (e: Exception) {
                Log.e(TAG, "detect failed", e)
                synchronized(lock) { lastError = e.toString() }
                try {
                    Thread.sleep(1000)
                } catch (_: InterruptedException) {
                    break
                }
            }
        }
    }

    /** Blocks until a result newer than [afterSeq] exists (or timeout), for streaming. */
    fun awaitNext(afterSeq: Long, timeoutMs: Long): JSONObject? {
        val deadline = System.currentTimeMillis() + timeoutMs
        synchronized(lock) {
            while (true) {
                latest?.let { if (it.optLong("seq") > afterSeq) return it }
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) return null
                lock.wait(left)
            }
        }
    }

    fun status(): JSONObject = synchronized(lock) {
        JSONObject()
            .put("running", running)
            .put("config", config?.let {
                JSONObject().put("cam", it.cam).put("fps", it.fps).put("model", it.model)
                    .put("size", it.size).put("thresh", it.thresh.toDouble())
            } ?: JSONObject.NULL)
            .put("latest", latest ?: JSONObject.NULL)
            .put("error", lastError ?: JSONObject.NULL)
            .put("models", JSONArray(engine.models()))
    }

    /** One frame from a camera, detected now. */
    fun once(cam: String?, model: String, size: Int, thresh: Float): JSONObject {
        val u = camera.acquireRaw(cam)
        try {
            var s = 0L
            val deadline = System.currentTimeMillis() + 8000
            while (System.currentTimeMillis() < deadline) {
                val f = u.nextRaw(s, deadline - System.currentTimeMillis()) ?: break
                s = f.seq
                if (f.time - u.openedAt < 800) continue // let exposure settle after opening
                val t0 = System.currentTimeMillis()
                val objects = engine.detect(f.nv21, f.width, f.height, 0, model, size, thresh)
                return JSONObject().put("camera", u.id).put("t", f.time)
                    .put("infer_ms", System.currentTimeMillis() - t0).put("objects", objects)
            }
            throw IllegalStateException("no frame from camera ${u.id}")
        } finally {
            u.releaseRaw()
        }
    }

    /** Detect on an uploaded JPEG/PNG (e.g. from a Pi camera). */
    fun image(bytes: ByteArray, model: String, size: Int, thresh: Float): JSONObject {
        val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size,
            BitmapFactory.Options().apply { inPreferredConfig = Bitmap.Config.ARGB_8888 })
            ?: throw IllegalArgumentException("not a decodable image")
        val rgba = ByteBuffer.allocate(bmp.byteCount).also { bmp.copyPixelsToBuffer(it) }.array()
        val t0 = System.currentTimeMillis()
        val objects = engine.detect(rgba, bmp.width, bmp.height, 1, model, size, thresh)
        return JSONObject().put("image", "${bmp.width}x${bmp.height}")
            .put("infer_ms", System.currentTimeMillis() - t0).put("objects", objects)
    }

    companion object {
        private const val TAG = "DashLinkDetector"
        private const val HOT_C = 80.0
        const val DEFAULT_MODEL = "yolo11n"
    }
}
