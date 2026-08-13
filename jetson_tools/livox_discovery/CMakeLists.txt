cmake_minimum_required(VERSION 3.10)
project(livox_discover)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_path(LIVOX_INCLUDE_DIR livox_lidar_api.h PATHS /usr/local/include /usr/include)
if(NOT LIVOX_INCLUDE_DIR)
  message(FATAL_ERROR "Livox SDK2 headers not found")
endif()

find_library(
  LIVOX_SDK_LIBRARY
  NAMES livox_lidar_sdk_shared livox_lidar_sdk_static
  PATHS /usr/local/lib /usr/lib /usr/lib/aarch64-linux-gnu
)
if(NOT LIVOX_SDK_LIBRARY)
  message(FATAL_ERROR "Livox SDK2 library not found")
endif()

add_executable(livox_discover main.cpp)
target_include_directories(livox_discover PRIVATE ${LIVOX_INCLUDE_DIR})
target_link_libraries(livox_discover PRIVATE ${LIVOX_SDK_LIBRARY} pthread dl)
