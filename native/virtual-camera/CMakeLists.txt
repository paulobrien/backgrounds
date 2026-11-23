cmake_minimum_required(VERSION 3.21)
project(BackgroundsVirtualCamera LANGUAGES CXX)
if(MSVC)
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>" CACHE STRING "MSVC runtime linkage" FORCE)
endif()

# Obtain Microsoft's DirectShow base classes from the Windows classic samples.
# Pass -DDIRECTSHOW_BASECLASSES_DIR=<path>/Samples/Win7Samples/multimedia/directshow/baseclasses
set(DIRECTSHOW_BASECLASSES_DIR "" CACHE PATH "Path to Microsoft DirectShow baseclasses sources")
if(NOT EXISTS "${DIRECTSHOW_BASECLASSES_DIR}/streams.h")
  message(FATAL_ERROR "Set DIRECTSHOW_BASECLASSES_DIR to the Windows classic samples DirectShow baseclasses folder")
endif()
file(GLOB BASECLASS_SOURCES CONFIGURE_DEPENDS "${DIRECTSHOW_BASECLASSES_DIR}/*.cpp")
add_library(directshow_baseclasses STATIC ${BASECLASS_SOURCES})
target_include_directories(directshow_baseclasses PUBLIC "${DIRECTSHOW_BASECLASSES_DIR}")
target_compile_definitions(directshow_baseclasses PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN)
target_compile_features(directshow_baseclasses PRIVATE cxx_std_17)
add_library(BackgroundsCamera SHARED camera.cpp exports.def)
target_compile_definitions(BackgroundsCamera PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN)
target_compile_features(BackgroundsCamera PRIVATE cxx_std_17)
target_link_libraries(BackgroundsCamera PRIVATE directshow_baseclasses strmiids winmm ole32 oleaut32 uuid advapi32)



