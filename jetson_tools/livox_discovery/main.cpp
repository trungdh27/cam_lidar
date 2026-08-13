#include "livox_lidar_api.h"
#include "livox_lidar_def.h"

#include <arpa/inet.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

struct DeviceResult {
  bool found = false;
  uint32_t handle = 0;
  uint8_t dev_type = 0;
  std::string model;
  std::string serial;
  std::string lidar_ip;
};

static std::atomic<bool> g_found(false);
static std::mutex g_mutex;
static DeviceResult g_device;

static std::string JsonEscape(const std::string& value) {
  std::ostringstream out;
  for (char c : value) {
    switch (c) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default: out << c; break;
    }
  }
  return out.str();
}

static std::string ModelFromDevType(uint8_t dev_type) {
  switch (dev_type) {
    case kLivoxLidarTypeMid360: return "MID360";
    case kLivoxLidarTypeMid360s: return "MID360S";
    default: return "UNKNOWN";
  }
}

static void LidarInfoChangeCallback(const uint32_t handle, const LivoxLidarInfo* info, void*) {
  if (info == nullptr) return;

  DeviceResult result;
  result.found = true;
  result.handle = handle;
  result.dev_type = info->dev_type;
  result.model = ModelFromDevType(info->dev_type);
  result.serial = info->sn;
  result.lidar_ip = info->lidar_ip;

  {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_device = result;
  }
  g_found.store(true);
}

static std::string BuildMid360FamilyConfig(const std::string& host_ip) {
  std::ostringstream ss;
  ss << R"({
  "master_sdk": true,
  "lidar_log_enable": false,
  "MID360": {
    "lidar_net_info": {
      "cmd_data_port": 56100,
      "push_msg_port": 56200,
      "point_data_port": 56300,
      "imu_data_port": 56400,
      "log_data_port": 56500
    },
    "host_net_info": [
      {
        "host_ip": ")" << host_ip << R"(",
        "multicast_ip": "224.1.1.5",
        "cmd_data_port": 56101,
        "push_msg_port": 56201,
        "point_data_port": 56301,
        "imu_data_port": 56401,
        "log_data_port": 56501
      }
    ]
  }
})";
  return ss.str();
}

static void PrintVersion() {
  LivoxLidarSdkVer version{};
  GetLivoxLidarSdkVer(&version);
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,"
            << "\"sdk_version\":\""
            << version.major << "." << version.minor << "." << version.patch
            << "\"}" << std::endl;
}

static void PrintResult(const DeviceResult& device, const std::string& host_ip,
                        const std::string& profile, const LivoxLidarSdkVer& version) {
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,\"found\":true,"
            << "\"profile\":\"" << JsonEscape(profile) << "\","
            << "\"host_ip\":\"" << JsonEscape(host_ip) << "\","
            << "\"model\":\"" << JsonEscape(device.model) << "\","
            << "\"serial\":\"" << JsonEscape(device.serial) << "\","
            << "\"lidar_ip\":\"" << JsonEscape(device.lidar_ip) << "\","
            << "\"dev_type\":" << static_cast<unsigned int>(device.dev_type) << ","
            << "\"handle\":" << device.handle << ","
            << "\"sdk_version\":\""
            << version.major << "." << version.minor << "." << version.patch
            << "\"}" << std::endl;
}

static void PrintNotFound(const std::string& host_ip, const std::string& profile,
                          const LivoxLidarSdkVer& version) {
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,\"found\":false,"
            << "\"profile\":\"" << JsonEscape(profile) << "\","
            << "\"host_ip\":\"" << JsonEscape(host_ip) << "\","
            << "\"sdk_version\":\""
            << version.major << "." << version.minor << "." << version.patch
            << "\"}" << std::endl;
}

int main(int argc, char* argv[]) {
  if (argc == 2 && std::string(argv[1]) == "--version") {
    PrintVersion();
    return 0;
  }

  if (argc < 3 || argc > 4) {
    std::cerr << "Usage:\n"
              << "  livox_discover --version\n"
              << "  livox_discover <host_ip> <mid360|mid360s> [timeout_sec]\n";
    return 1;
  }

  const std::string host_ip = argv[1];
  const std::string profile = argv[2];
  if (profile != "mid360" && profile != "mid360s") {
    std::cerr << "Unsupported profile: " << profile << std::endl;
    return 1;
  }

  int timeout_sec = (argc == 4) ? std::atoi(argv[3]) : 8;
  if (timeout_sec < 1 || timeout_sec > 60) {
    std::cerr << "Invalid timeout." << std::endl;
    return 1;
  }

  struct in_addr address{};
  if (inet_pton(AF_INET, host_ip.c_str(), &address) != 1) {
    std::cerr << "Invalid host IPv4: " << host_ip << std::endl;
    return 1;
  }

  const std::string config = BuildMid360FamilyConfig(host_ip);
  const std::string config_path =
      "/tmp/cam_lidar_livox_" + std::to_string(static_cast<long long>(getpid())) + ".json";

  {
    std::ofstream file(config_path);
    if (!file.is_open()) {
      std::cerr << "Unable to create config file." << std::endl;
      return 1;
    }
    file << config;
  }

  LivoxLidarSdkVer version{};
  GetLivoxLidarSdkVer(&version);

  if (!LivoxLidarSdkInit(config_path.c_str())) {
    std::remove(config_path.c_str());
    std::cerr << "LivoxLidarSdkInit failed." << std::endl;
    return 3;
  }

  SetLivoxLidarInfoChangeCallback(LidarInfoChangeCallback, nullptr);

  const int sleep_ms = 100;
  const int loops = timeout_sec * 1000 / sleep_ms;
  for (int i = 0; i < loops && !g_found.load(); ++i) {
    std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
  }

  DeviceResult result;
  if (g_found.load()) {
    std::lock_guard<std::mutex> lock(g_mutex);
    result = g_device;
  }

  LivoxLidarSdkUninit();
  std::remove(config_path.c_str());

  if (!result.found) {
    PrintNotFound(host_ip, profile, version);
    return 2;
  }

  PrintResult(result, host_ip, profile, version);
  return 0;
}
