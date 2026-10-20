#!/bin/sh
# The C++ libraries DashLink builds against, at the versions it was tested with. Run once before building.
set -e
cd "$(dirname "$0")/app/src/main/cpp"
[ -d whisper.cpp ] || git clone --depth 1 --branch v1.9.5 https://github.com/ggml-org/whisper.cpp.git
[ -d ncnn ] || git clone --depth 1 --branch 20260526 https://github.com/Tencent/ncnn.git  # CPU only: no Vulkan/glslang
echo "whisper.cpp and ncnn are in app/src/main/cpp."
echo "The object detector model isn't included (YOLO11 is AGPL-3.0, from Ultralytics): export yolo11n to ncnn"
echo "(pip install ultralytics; yolo export model=yolo11n.pt format=ncnn) and copy yolo11n.ncnn.{param,bin} to app/src/main/assets/."
