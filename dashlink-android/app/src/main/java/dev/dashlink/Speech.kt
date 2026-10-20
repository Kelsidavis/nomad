package dev.dashlink

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Android's on-device text-to-speech, either played on the phone or rendered to a WAV. */
class TtsEngine(private val context: Context) {

    @Volatile var ready = false
        private set
    private val pending = ConcurrentHashMap<String, CountDownLatch>()
    private val failed = ConcurrentHashMap.newKeySet<String>()
    private val lock = Any()

    private val tts: TextToSpeech = TextToSpeech(context) { status ->
        ready = status == TextToSpeech.SUCCESS
        if (ready) tts.language = Locale.US
    }

    init {
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String) {}
            override fun onDone(id: String) { pending.remove(id)?.countDown() }
            @Deprecated("Deprecated in Java")
            override fun onError(id: String) { failed.add(id); pending.remove(id)?.countDown() }
            override fun onError(id: String, errorCode: Int) = @Suppress("DEPRECATION") onError(id)
        })
    }

    val engine: String? get() = tts.defaultEngine

    /** Speak through the phone's own speaker. Blocks until finished. */
    fun speak(text: String, rate: Float, pitch: Float): Boolean = run(rate, pitch) { id ->
        tts.speak(text, TextToSpeech.QUEUE_ADD, Bundle(), id)
    }

    /** Render to a WAV file in the cache dir; the caller deletes it. */
    fun synthesize(text: String, rate: Float, pitch: Float): File? {
        val file = File(context.cacheDir, "tts-${UUID.randomUUID()}.wav")
        val ok = run(rate, pitch) { id -> tts.synthesizeToFile(text, Bundle(), file, id) }
        return if (ok && file.length() > 44) file else { file.delete(); null }
    }

    // Rate and pitch are engine-global, so utterances are serialized.
    private fun run(rate: Float, pitch: Float, start: (String) -> Int): Boolean = synchronized(lock) {
        if (!ready) return false
        val id = UUID.randomUUID().toString()
        val latch = CountDownLatch(1)
        pending[id] = latch
        tts.setSpeechRate(rate)
        tts.setPitch(pitch)
        if (start(id) != TextToSpeech.SUCCESS) {
            pending.remove(id)
            return false
        }
        val finished = latch.await(120, TimeUnit.SECONDS)
        return finished && !failed.remove(id)
    }

    fun shutdown() = tts.shutdown()
}

/** Android's speech recognizer listening on the phone's microphones. One request at a time. */
class SttEngine(private val context: Context) {

    private val main = Handler(Looper.getMainLooper())
    private val busy = Semaphore(1)

    val available: Boolean get() = SpeechRecognizer.isRecognitionAvailable(context)

    fun listen(language: String, preferOffline: Boolean, maxListenMs: Long): JSONObject {
        if (!busy.tryAcquire(maxListenMs, TimeUnit.MILLISECONDS)) {
            return JSONObject().put("ok", false).put("error", "busy")
        }
        val recognizer = AtomicReference<SpeechRecognizer?>()
        try {
            val result = LinkedBlockingQueue<JSONObject>(1)
            val started = System.currentTimeMillis()
            main.post {
                val rec = SpeechRecognizer.createSpeechRecognizer(context)
                recognizer.set(rec)
                rec.setRecognitionListener(Listener { result.offer(it) })
                val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
                    .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, preferOffline)
                    .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
                    .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
                rec.startListening(intent)
            }
            // Cap the listening window; the recognizer still delivers a result after stopListening.
            main.postDelayed({ recognizer.get()?.stopListening() }, maxListenMs)
            val out = result.poll(maxListenMs + 15_000, TimeUnit.MILLISECONDS)
                ?: JSONObject().put("ok", false).put("error", "timeout")
            return out.put("elapsed_ms", System.currentTimeMillis() - started)
        } finally {
            main.removeCallbacksAndMessages(null)
            main.post { recognizer.get()?.destroy() }
            busy.release()
        }
    }

    private class Listener(private val done: (JSONObject) -> Unit) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onError(error: Int) {
            done(JSONObject().put("ok", false).put("error", errorName(error)).put("code", error))
        }

        override fun onResults(results: Bundle) {
            val texts = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION).orEmpty()
            val scores = results.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES)
            val alts = JSONArray()
            texts.forEachIndexed { i, t ->
                alts.put(JSONObject().put("text", t).put("confidence", scores?.getOrNull(i)?.toDouble() ?: JSONObject.NULL))
            }
            done(JSONObject().put("ok", texts.isNotEmpty()).put("text", texts.firstOrNull() ?: "").put("alternatives", alts))
        }
    }

    companion object {
        fun errorName(code: Int) = when (code) {
            SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "network_timeout"
            SpeechRecognizer.ERROR_NETWORK -> "network"
            SpeechRecognizer.ERROR_AUDIO -> "audio"
            SpeechRecognizer.ERROR_SERVER -> "server"
            SpeechRecognizer.ERROR_CLIENT -> "client"
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "no_speech"
            SpeechRecognizer.ERROR_NO_MATCH -> "no_match"
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "recognizer_busy"
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "insufficient_permissions"
            else -> "error_$code"
        }
    }
}
