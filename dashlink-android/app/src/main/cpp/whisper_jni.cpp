#include <jni.h>
#include <string>
#include <android/log.h>
#include "whisper.h"

#define TAG "DashLinkWhisper"

extern "C" {

JNIEXPORT jlong JNICALL
Java_dev_dashlink_WhisperEngine_nativeInit(JNIEnv *env, jclass, jstring jpath) {
    const char *path = env->GetStringUTFChars(jpath, nullptr);
    whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;
    whisper_context *ctx = whisper_init_from_file_with_params(path, cparams);
    if (!ctx) __android_log_print(ANDROID_LOG_ERROR, TAG, "failed to load %s", path);
    env->ReleaseStringUTFChars(jpath, path);
    return reinterpret_cast<jlong>(ctx);
}

JNIEXPORT void JNICALL
Java_dev_dashlink_WhisperEngine_nativeFree(JNIEnv *, jclass, jlong handle) {
    if (handle) whisper_free(reinterpret_cast<whisper_context *>(handle));
}

// Returns the transcript as UTF-8 bytes (NewStringUTF chokes on 4-byte UTF-8), or null on failure.
JNIEXPORT jbyteArray JNICALL
Java_dev_dashlink_WhisperEngine_nativeTranscribe(JNIEnv *env, jclass, jlong handle,
                                                jfloatArray jpcm, jstring jlang,
                                                jint threads, jstring jprompt) {
    auto *ctx = reinterpret_cast<whisper_context *>(handle);
    if (!ctx) return nullptr;

    whisper_full_params p = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    p.n_threads = threads;
    p.print_progress = false;
    p.print_realtime = false;
    p.print_timestamps = false;
    p.print_special = false;
    p.no_timestamps = true;
    p.no_context = true;
    p.suppress_blank = true;

    const char *lang = env->GetStringUTFChars(jlang, nullptr);
    std::string lang_s(lang);
    env->ReleaseStringUTFChars(jlang, lang);
    p.language = lang_s.c_str();

    // An initial prompt biases vocabulary, e.g. street names or "OBD, coolant, RPM".
    std::string prompt_s;
    if (jprompt) {
        const char *pr = env->GetStringUTFChars(jprompt, nullptr);
        prompt_s = pr;
        env->ReleaseStringUTFChars(jprompt, pr);
        if (!prompt_s.empty()) p.initial_prompt = prompt_s.c_str();
    }

    jsize n = env->GetArrayLength(jpcm);

    // Whisper pads every clip to 30 s (1500 encoder frames, 50 per second). For short voice
    // commands, encode only what was sent plus a little margin. That is most of the cost here.
    int frames = (int) ((int64_t) n * 50 / WHISPER_SAMPLE_RATE) + 128;
    if (frames < 1500) p.audio_ctx = frames;
    // A shortened context makes whisper prone to repeating itself; cap output to what the
    // clip could plausibly contain (speech is ~3-4 tokens/s) so a loop ends quickly.
    p.max_tokens = (int) ((int64_t) n * 6 / WHISPER_SAMPLE_RATE) + 16;
    p.single_segment = true;
    jfloat *pcm = env->GetFloatArrayElements(jpcm, nullptr);
    int rc = whisper_full(ctx, p, pcm, n);
    env->ReleaseFloatArrayElements(jpcm, pcm, JNI_ABORT);
    if (rc != 0) return nullptr;

    std::string text;
    for (int i = 0; i < whisper_full_n_segments(ctx); i++) {
        text += whisper_full_get_segment_text(ctx, i);
    }
    jbyteArray out = env->NewByteArray((jsize) text.size());
    env->SetByteArrayRegion(out, 0, (jsize) text.size(), reinterpret_cast<const jbyte *>(text.data()));
    return out;
}

}
