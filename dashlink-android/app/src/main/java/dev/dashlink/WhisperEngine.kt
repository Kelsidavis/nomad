package dev.dashlink

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * whisper.cpp for audio recorded elsewhere (the Pi's ReSpeaker mics).
 * Models live in /sdcard/Android/data/dev.dashlink/files/models/ggml-<name>.bin (adb push them there).
 */
class WhisperEngine(context: Context) {

    val modelDir: File = context.getExternalFilesDir("models")!!.also { it.mkdirs() }

    private var handle = 0L
    private var loaded: String? = null

    fun models(): List<String> =
        modelDir.listFiles { f -> f.name.startsWith("ggml-") && f.name.endsWith(".bin") }
            ?.map { it.name.removePrefix("ggml-").removeSuffix(".bin") }
            ?.sorted()
            .orEmpty()

    /** First preferred model that is installed. */
    fun defaultModel(): String? = PREFERRED.firstOrNull { it in models() } ?: models().firstOrNull()

    @Synchronized
    fun transcribe(audio: ByteArray, model: String?, language: String, prompt: String?): JSONObject {
        val name = model ?: defaultModel()
            ?: return JSONObject().put("ok", false).put("error", "no models in ${modelDir.path}")
        val loadMs = load(name)
            ?: return JSONObject().put("ok", false).put("error", "could not load model $name")

        val pcm = Wav.padded(Wav.toMono16k(audio))
        val start = System.currentTimeMillis()
        val bytes = nativeTranscribe(handle, pcm, language, THREADS, prompt)
            ?: return JSONObject().put("ok", false).put("error", "whisper_full failed")
        return JSONObject()
            .put("ok", true)
            .put("text", collapseRepeats(String(bytes, Charsets.UTF_8).trim()))
            .put("model", name)
            .put("audio_ms", pcm.size * 1000L / Wav.RATE)
            .put("elapsed_ms", System.currentTimeMillis() - start)
            .put("load_ms", loadMs)
    }

    /** Loads the model if it isn't already; returns time spent loading, or null on failure. */
    private fun load(name: String): Long? {
        if (loaded == name && handle != 0L) return 0
        val file = File(modelDir, "ggml-$name.bin")
        if (!file.isFile) return null
        free()
        val start = System.currentTimeMillis()
        handle = nativeInit(file.path)
        if (handle == 0L) return null
        loaded = name
        return System.currentTimeMillis() - start
    }

    @Synchronized
    fun free() {
        if (handle != 0L) nativeFree(handle)
        handle = 0L
        loaded = null
    }

    companion object {
        /**
         * Short clips sometimes decode as the same phrase over and over ("navigate home navigate
         * home ..."). Keep the first occurrence of any phrase repeated back-to-back.
         */
        fun collapseRepeats(text: String): String {
            val words = text.split(Regex("\\s+")).filter { it.isNotEmpty() }.toMutableList()
            fun key(w: String) = w.lowercase().trim { !it.isLetterOrDigit() }
            // Whole output is one phrase looping, usually cut off mid-repeat by the token cap.
            for (p in 1..words.size / 2) {
                if (words.size >= p + minOf(p, 2) && (p until words.size).all { key(words[it]) == key(words[it - p]) }) {
                    return words.take(p).joinToString(" ")
                }
            }
            var n = words.size / 2
            while (n >= 1) {
                var i = 0
                while (i + 2 * n <= words.size) {
                    val same = (0 until n).all { key(words[i + it]) == key(words[i + n + it]) }
                    if (same) {
                        repeat(n) { words.removeAt(i + n) }
                    } else {
                        i++
                    }
                }
                n--
            }
            return words.joinToString(" ")
        }

        // Measured on the G7's SDM845; see README.
        private val PREFERRED = listOf("base.en", "base.en-q8_0", "tiny.en", "small.en-q8_0")
        private const val THREADS = 4

        init {
            System.loadLibrary("dashlink_whisper")
        }

        @JvmStatic private external fun nativeInit(path: String): Long
        @JvmStatic private external fun nativeFree(handle: Long)
        @JvmStatic private external fun nativeTranscribe(
            handle: Long, pcm: FloatArray, language: String, threads: Int, prompt: String?,
        ): ByteArray?
    }
}

/** Minimal WAV reader: PCM16/PCM32/float32, any channel count and rate, to 16 kHz mono float. */
object Wav {
    const val RATE = 16000

    /** Lead-in/out silence; clips cut right at the first word lose that word otherwise. */
    fun padded(pcm: FloatArray, ms: Int = 300): FloatArray {
        val pad = RATE * ms / 1000
        return FloatArray(pcm.size + 2 * pad).also { System.arraycopy(pcm, 0, it, pad, pcm.size) }
    }

    fun toMono16k(data: ByteArray): FloatArray {
        val buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
        if (data.size < 12 || String(data, 0, 4) != "RIFF" || String(data, 8, 4) != "WAVE") {
            // Not a WAV: treat as raw 16 kHz mono s16le.
            return mono(buf, 0, data.size, 1, 1, 16).let { resample(it, RATE) }
        }
        var format = 1
        var channels = 1
        var rate = RATE
        var bits = 16
        var pos = 12
        while (pos + 8 <= data.size) {
            val id = String(data, pos, 4)
            val size = buf.getInt(pos + 4)
            val body = pos + 8
            when (id) {
                "fmt " -> {
                    format = buf.getShort(body).toInt() and 0xffff
                    channels = buf.getShort(body + 2).toInt()
                    rate = buf.getInt(body + 4)
                    bits = buf.getShort(body + 14).toInt()
                    if (format == 0xFFFE) format = buf.getShort(body + 24).toInt() // WAVE_FORMAT_EXTENSIBLE
                }
                "data" -> {
                    // Streaming writers (arecord to a pipe) leave the size as 0 or 0xFFFFFFFF.
                    val end = if (size <= 0 || body + size > data.size) data.size else body + size
                    return resample(mono(buf, body, end, channels, format, bits), rate)
                }
            }
            pos = body + size + (size and 1)
        }
        throw IllegalArgumentException("WAV has no data chunk")
    }

    private fun mono(buf: ByteBuffer, start: Int, end: Int, channels: Int, format: Int, bits: Int): FloatArray {
        val bytes = bits / 8
        val frames = (end - start) / (bytes * channels)
        val out = FloatArray(frames)
        for (f in 0 until frames) {
            var sum = 0f
            for (c in 0 until channels) {
                val at = start + (f * channels + c) * bytes
                sum += when {
                    format == 3 && bits == 32 -> buf.getFloat(at)
                    bits == 16 -> buf.getShort(at) / 32768f
                    bits == 32 -> buf.getInt(at) / 2147483648f
                    bits == 24 -> ((buf.get(at).toInt() and 0xff) or
                        ((buf.get(at + 1).toInt() and 0xff) shl 8) or
                        (buf.get(at + 2).toInt() shl 16)) / 8388608f
                    else -> throw IllegalArgumentException("unsupported WAV: format $format, $bits bits")
                }
            }
            out[f] = sum / channels
        }
        return out
    }

    private fun resample(x: FloatArray, from: Int): FloatArray {
        if (from == RATE || x.isEmpty()) return x
        val n = (x.size.toLong() * RATE / from).toInt()
        val step = from.toDouble() / RATE
        return FloatArray(n) { i ->
            val p = i * step
            val j = p.toInt().coerceAtMost(x.size - 1)
            val k = (j + 1).coerceAtMost(x.size - 1)
            val t = (p - j).toFloat()
            x[j] * (1 - t) + x[k] * t
        }
    }
}
