// YOLO11/YOLOv8 object detection with ncnn on the CPU.
// Post-processing follows nihui/ncnn-android-yolo11 (yolo11_det.cpp), BSD-3-Clause.
#include <jni.h>
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <vector>
#include <cstdlib>
#include <android/log.h>
#include "net.h"

#define TAG "DashLinkDetect"

namespace {

struct Box {
    float x, y, w, h;
    int label;
    float prob;
};

float iou(const Box& a, const Box& b) {
    float x0 = std::max(a.x, b.x), y0 = std::max(a.y, b.y);
    float x1 = std::min(a.x + a.w, b.x + b.w), y1 = std::min(a.y + a.h, b.y + b.h);
    float inter = std::max(0.f, x1 - x0) * std::max(0.f, y1 - y0);
    return inter / (a.w * a.h + b.w * b.h - inter);
}

void proposals(const ncnn::Mat& pred, int stride, int w, int h, float thresh, std::vector<Box>& out) {
    const int gx = w / stride, gy = h / stride;
    const int reg = 16;
    const int ncls = pred.w - reg * 4;
    for (int y = 0; y < gy; y++) {
        for (int x = 0; x < gx; x++) {
            const ncnn::Mat row = pred.row_range(y * gx + x, 1);
            int label = -1;
            float score = -FLT_MAX;
            const ncnn::Mat sc = row.range(reg * 4, ncls);
            for (int k = 0; k < ncls; k++) {
                if (sc[k] > score) {
                    score = sc[k];
                    label = k;
                }
            }
            score = 1.f / (1.f + expf(-score));
            if (score < thresh) continue;

            // Distribution focal loss: softmax over 16 bins per side; expected bin = distance.
            const float* bbox = (const float*) row;
            float ltrb[4];
            for (int k = 0; k < 4; k++) {
                const float* v = bbox + k * reg;
                float mx = -FLT_MAX, sum = 0.f, d = 0.f;
                for (int l = 0; l < reg; l++) mx = std::max(mx, v[l]);
                for (int l = 0; l < reg; l++) sum += expf(v[l] - mx);
                for (int l = 0; l < reg; l++) d += l * expf(v[l] - mx) / sum;
                ltrb[k] = d * stride;
            }
            float cx = (x + 0.5f) * stride, cy = (y + 0.5f) * stride;
            out.push_back({cx - ltrb[0], cy - ltrb[1], ltrb[0] + ltrb[2], ltrb[1] + ltrb[3], label, score});
        }
    }
}

std::vector<Box> detect(ncnn::Net& net, int img_w, int img_h, const unsigned char* rgb, int target, float thresh) {
    int w = img_w, h = img_h;
    float scale;
    if (w > h) {
        scale = (float) target / w;
        w = target;
        h = (int) (h * scale);
    } else {
        scale = (float) target / h;
        h = target;
        w = (int) (w * scale);
    }
    ncnn::Mat in = ncnn::Mat::from_pixels_resize(rgb, ncnn::Mat::PIXEL_RGB, img_w, img_h, w, h);
    int wpad = (w + 31) / 32 * 32 - w, hpad = (h + 31) / 32 * 32 - h;
    ncnn::Mat in_pad;
    ncnn::copy_make_border(in, in_pad, hpad / 2, hpad - hpad / 2, wpad / 2, wpad - wpad / 2, ncnn::BORDER_CONSTANT, 114.f);
    const float norm[3] = {1 / 255.f, 1 / 255.f, 1 / 255.f};
    in_pad.substract_mean_normalize(0, norm);

    ncnn::Extractor ex = net.create_extractor();
    ex.input("in0", in_pad);
    ncnn::Mat out;
    ex.extract("out0", out);

    std::vector<Box> props;
    int offset = 0;
    for (int stride : {8, 16, 32}) {
        int n = (in_pad.w / stride) * (in_pad.h / stride);
        proposals(out.row_range(offset, n), stride, in_pad.w, in_pad.h, thresh, props);
        offset += n;
    }
    std::sort(props.begin(), props.end(), [](const Box& a, const Box& b) { return a.prob > b.prob; });
    std::vector<Box> keep;
    for (const Box& p : props) {
        bool ok = true;
        for (const Box& k : keep) {
            if (k.label == p.label && iou(k, p) > 0.45f) {
                ok = false;
                break;
            }
        }
        if (ok) keep.push_back(p);
    }
    for (Box& b : keep) {
        float x0 = std::clamp((b.x - wpad / 2) / scale, 0.f, (float) img_w - 1);
        float y0 = std::clamp((b.y - hpad / 2) / scale, 0.f, (float) img_h - 1);
        float x1 = std::clamp((b.x + b.w - wpad / 2) / scale, 0.f, (float) img_w - 1);
        float y1 = std::clamp((b.y + b.h - hpad / 2) / scale, 0.f, (float) img_h - 1);
        b = {x0, y0, x1 - x0, y1 - y0, b.label, b.prob};
    }
    return keep;
}

} // namespace

extern "C" {

// LLVM's OpenMP runtime aborted (__kmp_abort_process) on first use inside the app process,
// most likely while probing CPU affinity, which the app sandbox restricts. Turn that off
// before the runtime initializes. (ncnn's SIMPLEOMP avoids the crash but measured 4x slower.)
JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM*, void*) {
    setenv("KMP_AFFINITY", "disabled", 1);
    setenv("KMP_WARNINGS", "0", 1);
    setenv("KMP_DUPLICATE_LIB_OK", "TRUE", 1);
    setenv("KMP_INIT_AT_FORK", "FALSE", 1);
    return JNI_VERSION_1_6;
}

JNIEXPORT jlong JNICALL
Java_dev_dashlink_DetectorEngine_nativeInit(JNIEnv* env, jclass, jstring jparam, jstring jbin, jint threads) {
    const char* param = env->GetStringUTFChars(jparam, nullptr);
    const char* bin = env->GetStringUTFChars(jbin, nullptr);
    auto* net = new ncnn::Net();
    net->opt.num_threads = threads;
    net->opt.lightmode = true;
    int rc = net->load_param(param);
    if (rc == 0) rc = net->load_model(bin);
    if (rc != 0) {
        __android_log_print(ANDROID_LOG_ERROR, TAG, "failed to load %s / %s", param, bin);
        delete net;
        net = nullptr;
    }
    env->ReleaseStringUTFChars(jparam, param);
    env->ReleaseStringUTFChars(jbin, bin);
    return reinterpret_cast<jlong>(net);
}

JNIEXPORT void JNICALL
Java_dev_dashlink_DetectorEngine_nativeFree(JNIEnv*, jclass, jlong handle) {
    delete reinterpret_cast<ncnn::Net*>(handle);
}

// format: 0 = NV21 (camera frames), 1 = RGBA (decoded bitmaps).
// Returns [x, y, w, h, label, prob] per object, in source-image pixels.
JNIEXPORT jfloatArray JNICALL
Java_dev_dashlink_DetectorEngine_nativeDetect(JNIEnv* env, jclass, jlong handle, jbyteArray jpixels, jint w, jint h,
                                              jint format, jint target, jfloat thresh) {
    auto* net = reinterpret_cast<ncnn::Net*>(handle);
    if (!net) return nullptr;

    jbyte* pixels = env->GetByteArrayElements(jpixels, nullptr);
    std::vector<unsigned char> rgb((size_t) w * h * 3);
    if (format == 0) {
        ncnn::yuv420sp2rgb(reinterpret_cast<const unsigned char*>(pixels), w, h, rgb.data());
    } else {
        const auto* src = reinterpret_cast<const unsigned char*>(pixels);
        for (size_t i = 0, n = (size_t) w * h; i < n; i++) {
            rgb[i * 3] = src[i * 4];
            rgb[i * 3 + 1] = src[i * 4 + 1];
            rgb[i * 3 + 2] = src[i * 4 + 2];
        }
    }
    env->ReleaseByteArrayElements(jpixels, pixels, JNI_ABORT);

    std::vector<Box> boxes = detect(*net, w, h, rgb.data(), target, thresh);

    jfloatArray out = env->NewFloatArray((jsize) boxes.size() * 6);
    std::vector<float> flat;
    flat.reserve(boxes.size() * 6);
    for (const Box& b : boxes) {
        flat.insert(flat.end(), {b.x, b.y, b.w, b.h, (float) b.label, b.prob});
    }
    env->SetFloatArrayRegion(out, 0, (jsize) flat.size(), flat.data());
    return out;
}

}
