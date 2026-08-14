cmake_minimum_required(VERSION 3.10)
project(livox_stream LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" LIVOX_SYSTEM_PROCESSOR)
if(NOT LIVOX_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64)$")
  message(FATAL_ERROR
    "livox_stream must be built natively on Jetson aarch64; "
    "CMAKE_SYSTEM_PROCESSOR=${CMAKE_SYSTEM_PROCESSOR}"
  )
endif()

set(LIVOX_SDK_ROOT "" CACHE PATH "Optional Livox SDK2 installation prefix")
set(LIVOX_INCLUDE_HINTS /usr/local/include /usr/include)
set(LIVOX_LIBRARY_HINTS
  /usr/local/lib
  /usr/local/lib64
  /usr/lib/aarch64-linux-gnu
  /usr/lib
)
if(LIVOX_SDK_ROOT)
  list(INSERT LIVOX_INCLUDE_HINTS 0 "${LIVOX_SDK_ROOT}/include")
  list(INSERT LIVOX_LIBRARY_HINTS 0
    "${LIVOX_SDK_ROOT}/lib"
    "${LIVOX_SDK_ROOT}/lib64"
  )
endif()

find_path(LIVOX_API_INCLUDE_DIR livox_lidar_api.h
  HINTS ${LIVOX_INCLUDE_HINTS} NO_DEFAULT_PATH)
find_path(LIVOX_DEF_INCLUDE_DIR livox_lidar_def.h
  HINTS ${LIVOX_INCLUDE_HINTS} NO_DEFAULT_PATH)
if(NOT LIVOX_API_INCLUDE_DIR OR NOT LIVOX_DEF_INCLUDE_DIR)
  message(FATAL_ERROR "Livox SDK2 headers were not found")
endif()

find_library(LIVOX_SDK_LIBRARY
  NAMES livox_lidar_sdk_shared livox_lidar_sdk_static
  HINTS ${LIVOX_LIBRARY_HINTS} NO_DEFAULT_PATH)
if(NOT LIVOX_SDK_LIBRARY)
  message(FATAL_ERROR "Livox SDK2 shared/static library was not found")
endif()

find_package(Threads REQUIRED)
get_filename_component(LIVOX_SDK_LIBRARY_DIR "${LIVOX_SDK_LIBRARY}" DIRECTORY)

add_executable(livox_stream main.cpp)
target_include_directories(livox_stream PRIVATE
  ${LIVOX_API_INCLUDE_DIR}
  ${LIVOX_DEF_INCLUDE_DIR}
)
target_link_libraries(livox_stream PRIVATE
  ${LIVOX_SDK_LIBRARY}
  Threads::Threads
  ${CMAKE_DL_LIBS}
)
target_compile_options(livox_stream PRIVATE -Wall -Wextra -Wpedantic)
set_target_properties(livox_stream PROPERTIES
  BUILD_RPATH "${LIVOX_SDK_LIBRARY_DIR}"
  INSTALL_RPATH "${LIVOX_SDK_LIBRARY_DIR}"
)
