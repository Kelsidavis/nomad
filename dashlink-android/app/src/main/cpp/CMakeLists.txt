cmake_minimum_required(VERSION 3.22)
project(dashlink_whisper C CXX)

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
# SDM845 has fp16 arithmetic but no dotprod (dotprod SIGILLs there).
set(GGML_CPU_ARM_ARCH "armv8.2-a+fp16" CACHE STRING "" FORCE)

add_subdirectory(whisper.cpp)

add_library(dashlink_whisper SHARED whisper_jni.cpp)
target_link_libraries(dashlink_whisper whisper log)

# Object detection: ncnn on the CPU. The Vulkan GPU path measured 3-4x slower on the Adreno 630.
set(NCNN_VULKAN OFF CACHE BOOL "" FORCE)
set(NCNN_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(NCNN_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(NCNN_BUILD_BENCHMARK OFF CACHE BOOL "" FORCE)
set(NCNN_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(NCNN_SHARED_LIB OFF CACHE BOOL "" FORCE)
set(NCNN_DISABLE_RTTI OFF CACHE BOOL "" FORCE)
set(NCNN_DISABLE_EXCEPTION OFF CACHE BOOL "" FORCE)
add_subdirectory(ncnn)

add_library(dashlink_detect SHARED detector_jni.cpp)
target_link_libraries(dashlink_detect ncnn log)
