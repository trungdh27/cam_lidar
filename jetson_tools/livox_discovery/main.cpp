#include "livox_lidar_api.h"
#include "livox_lidar_def.h"

#include <arpa/inet.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>

namespace {

// Livox SDK2 uses UDP 56000 for discovery; its JSON config schema does not
// expose a separate discovery-port field.
constexpr std::uint16_t kDiscoveryPort = 56000;
constexpr std::uint16_t kLidarCommandPort = 56100;
constexpr std::uint16_t kLidarPushPort = 56200;
constexpr std::uint16_t kLidarPointCloudPort = 56300;
constexpr std::uint16_t kLidarImuPort = 56400;
constexpr std::uint16_t kLidarLogPort = 56500;
constexpr std::uint16_t kHostCommandPort = 56101;
constexpr std::uint16_t kHostPushPort = 56201;
constexpr std::uint16_t kHostPointCloudPort = 56301;
constexpr std::uint16_t kHostImuPort = 56401;
constexpr std::uint16_t kHostLogPort = 56501;
constexpr std::uint32_t kLidarLogCacheSizeMb = 500;

struct Options {
  std::string host_ip;
  std::string expected_lidar_ip;
  std::string model;
  int timeout_sec = 8;
};

struct DeviceResult {
  bool found = false;
  std::uint32_t handle = 0;
  std::uint8_t dev_type = 0;
  std::string model;
  std::string serial;
  std::string lidar_ip;
};

std::atomic<bool> g_match_found(false);
std::atomic<bool> g_mismatch_found(false);
std::mutex g_mutex;
DeviceResult g_matched_device;
DeviceResult g_mismatched_device;
Options g_options;

std::string JsonEscape(const std::string& value) {
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

std::string ModelFromDevType(std::uint8_t dev_type) {
  switch (dev_type) {
    case kLivoxLidarTypeMid360: return "MID360";
    case kLivoxLidarTypeMid360s: return "MID360S";
    default: return "UNKNOWN";
  }
}

std::string SdkVersionText(const LivoxLidarSdkVer& version) {
  std::ostringstream out;
  out << version.major << "." << version.minor << "." << version.patch;
  return out.str();
}

bool IsValidIpv4(const std::string& value) {
  struct in_addr address {};
  return inet_pton(AF_INET, value.c_str(), &address) == 1;
}

bool ParseTimeout(const std::string& value, int* timeout_sec) {
  if (timeout_sec == nullptr || value.empty()) return false;
  errno = 0;
  char* end = nullptr;
  const long parsed = std::strtol(value.c_str(), &end, 10);
  if (errno != 0 || end == value.c_str() || *end != '\0') return false;
  if (parsed < 1 || parsed > 60) return false;
  *timeout_sec = static_cast<int>(parsed);
  return true;
}

void PrintUsage() {
  std::cerr
      << "Usage:\n"
      << "  livox_discover --version\n"
      << "  livox_discover --host-ip <IPv4> --expected-lidar-ip <IPv4> "
         "--model <MID360|MID360S> --timeout <1..60>\n";
}

void PrintError(const std::string& error_type, const std::string& message,
                const std::string& sdk_version = "") {
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":false,\"found\":false,"
            << "\"error_type\":\"" << JsonEscape(error_type) << "\","
            << "\"error\":\"" << JsonEscape(message) << "\"";
  if (!sdk_version.empty()) {
    std::cout << ",\"sdk_version\":\"" << JsonEscape(sdk_version) << "\"";
  }
  std::cout << "}" << std::endl;
}

bool ParseOptions(int argc, char* argv[], Options* options,
                  std::string* error) {
  if (options == nullptr || error == nullptr) return false;

  bool host_ip_set = false;
  bool expected_ip_set = false;
  bool model_set = false;
  bool timeout_set = false;

  for (int index = 1; index < argc; ++index) {
    const std::string option = argv[index];
    if (option != "--host-ip" && option != "--expected-lidar-ip" &&
        option != "--model" && option != "--timeout") {
      *error = "Unknown option: " + option;
      return false;
    }
    if (index + 1 >= argc) {
      *error = "Missing value for " + option;
      return false;
    }

    const std::string value = argv[++index];
    if (option == "--host-ip") {
      options->host_ip = value;
      host_ip_set = true;
    } else if (option == "--expected-lidar-ip") {
      options->expected_lidar_ip = value;
      expected_ip_set = true;
    } else if (option == "--model") {
      options->model = value;
      model_set = true;
    } else {
      if (!ParseTimeout(value, &options->timeout_sec)) {
        *error = "Timeout must be an integer between 1 and 60";
        return false;
      }
      timeout_set = true;
    }
  }

  if (!host_ip_set || !expected_ip_set || !model_set || !timeout_set) {
    *error = "--host-ip, --expected-lidar-ip, --model, and --timeout are required";
    return false;
  }
  if (!IsValidIpv4(options->host_ip)) {
    *error = "Invalid host IPv4: " + options->host_ip;
    return false;
  }
  if (!IsValidIpv4(options->expected_lidar_ip)) {
    *error = "Invalid expected LiDAR IPv4: " + options->expected_lidar_ip;
    return false;
  }
  if (options->model != "MID360" && options->model != "MID360S") {
    *error = "Unsupported model: " + options->model;
    return false;
  }
  return true;
}

std::string BuildModelConfig(const std::string& host_ip,
                             const std::string& config_section,
                             bool include_multicast_ip) {
  std::ostringstream config;
  config << "{\n"
         << "  \"master_sdk\": true,\n"
         << "  \"lidar_log_enable\": false,\n"
         << "  \"lidar_log_cache_size_MB\": " << kLidarLogCacheSizeMb << ",\n"
         << "  \"lidar_log_path\": \"./\",\n"
         << "  \"" << config_section << "\": {\n"
         << "    \"lidar_net_info\": {\n"
         << "      \"cmd_data_port\": " << kLidarCommandPort << ",\n"
         << "      \"push_msg_port\": " << kLidarPushPort << ",\n"
         << "      \"point_data_port\": " << kLidarPointCloudPort << ",\n"
         << "      \"imu_data_port\": " << kLidarImuPort << ",\n"
         << "      \"log_data_port\": " << kLidarLogPort << "\n"
         << "    },\n"
         << "    \"host_net_info\": [{\n"
         << "      \"host_ip\": \"" << JsonEscape(host_ip) << "\",\n";
  if (include_multicast_ip) {
    config << "      \"multicast_ip\": \"224.1.1.5\",\n";
  }
  config << "      \"cmd_data_port\": " << kHostCommandPort << ",\n"
         << "      \"push_msg_port\": " << kHostPushPort << ",\n"
         << "      \"point_data_port\": " << kHostPointCloudPort << ",\n"
         << "      \"imu_data_port\": " << kHostImuPort << ",\n"
         << "      \"log_data_port\": " << kHostLogPort << "\n"
         << "    }]\n"
         << "  }\n"
         << "}";
  return config.str();
}

std::string BuildMid360Config(const std::string& host_ip) {
  return BuildModelConfig(host_ip, "MID360", true);
}

std::string BuildMid360SConfig(const std::string& host_ip) {
  return BuildModelConfig(host_ip, "Mid360s", false);
}

std::string BuildSdkConfig(const Options& options) {
  if (options.model == "MID360") {
    return BuildMid360Config(options.host_ip);
  }
  if (options.model == "MID360S") {
    return BuildMid360SConfig(options.host_ip);
  }
  return "";
}

void LidarInfoChangeCallback(const std::uint32_t handle,
                             const LivoxLidarInfo* info, void*) {
  if (info == nullptr) return;

  DeviceResult result;
  result.found = true;
  result.handle = handle;
  result.dev_type = info->dev_type;
  result.model = ModelFromDevType(info->dev_type);
  result.serial = info->sn;
  result.lidar_ip = info->lidar_ip;

  const bool ip_matches = result.lidar_ip == g_options.expected_lidar_ip;
  const bool model_matches = result.model == g_options.model;
  std::lock_guard<std::mutex> lock(g_mutex);
  if (ip_matches && model_matches) {
    g_matched_device = result;
    g_match_found.store(true);
  } else {
    g_mismatched_device = result;
    g_mismatch_found.store(true);
  }
}

void PrintVersion() {
  LivoxLidarSdkVer version {};
  GetLivoxLidarSdkVer(&version);
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,"
            << "\"sdk_version\":\"" << SdkVersionText(version) << "\"}"
            << std::endl;
}

void PrintFound(const DeviceResult& device, const Options& options,
                const LivoxLidarSdkVer& version) {
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,\"found\":true,\"mismatch\":false,"
            << "\"host_ip\":\"" << JsonEscape(options.host_ip) << "\","
            << "\"expected_lidar_ip\":\""
            << JsonEscape(options.expected_lidar_ip) << "\","
            << "\"model\":\"" << JsonEscape(device.model) << "\","
            << "\"dev_type\":" << static_cast<unsigned int>(device.dev_type) << ","
            << "\"serial\":\"" << JsonEscape(device.serial) << "\","
            << "\"lidar_ip\":\"" << JsonEscape(device.lidar_ip) << "\","
            << "\"handle\":" << device.handle << ","
            << "\"discovery_port\":" << kDiscoveryPort << ","
            << "\"sdk_version\":\"" << SdkVersionText(version) << "\"}"
            << std::endl;
}

void PrintNotFound(const Options& options, const DeviceResult* mismatch,
                   const LivoxLidarSdkVer& version) {
  const bool has_mismatch = mismatch != nullptr && mismatch->found;
  std::cout << "LIVOX_DISCOVERY_JSON={"
            << "\"ok\":true,\"found\":false,"
            << "\"mismatch\":" << (has_mismatch ? "true" : "false") << ","
            << "\"reason\":\""
            << (has_mismatch ? "detected_device_does_not_match" : "timeout")
            << "\","
            << "\"host_ip\":\"" << JsonEscape(options.host_ip) << "\","
            << "\"expected_lidar_ip\":\""
            << JsonEscape(options.expected_lidar_ip) << "\","
            << "\"expected_model\":\"" << JsonEscape(options.model) << "\","
            << "\"discovery_port\":" << kDiscoveryPort << ","
            << "\"sdk_version\":\"" << SdkVersionText(version) << "\"";
  if (has_mismatch) {
    std::cout << ",\"detected_model\":\"" << JsonEscape(mismatch->model) << "\","
              << "\"detected_dev_type\":"
              << static_cast<unsigned int>(mismatch->dev_type) << ","
              << "\"detected_serial\":\"" << JsonEscape(mismatch->serial) << "\","
              << "\"detected_lidar_ip\":\""
              << JsonEscape(mismatch->lidar_ip) << "\","
              << "\"detected_handle\":" << mismatch->handle;
  }
  std::cout << "}" << std::endl;
}

class ScopedConfigFile {
 public:
  explicit ScopedConfigFile(std::string path) : path_(std::move(path)) {}
  ~ScopedConfigFile() { std::remove(path_.c_str()); }

  const std::string& path() const { return path_; }

  bool Write(const std::string& content) const {
    std::ofstream file(path_);
    if (!file.is_open()) return false;
    file << content;
    return file.good();
  }

 private:
  std::string path_;
};

}  // namespace

int main(int argc, char* argv[]) {
  if (argc == 2 && std::string(argv[1]) == "--version") {
    PrintVersion();
    return 0;
  }

  Options options;
  std::string input_error;
  if (!ParseOptions(argc, argv, &options, &input_error)) {
    PrintUsage();
    PrintError("input", input_error);
    return 1;
  }
  g_options = options;

  const std::string config_path =
      "/tmp/cam_lidar_livox_" +
      std::to_string(static_cast<long long>(getpid())) + ".json";
  const ScopedConfigFile config_file(config_path);
  const std::string sdk_config = BuildSdkConfig(options);
  if (sdk_config.empty()) {
    PrintError("input", "Unsupported model for SDK config: " + options.model);
    return 1;
  }
  if (!config_file.Write(sdk_config)) {
    PrintError("config", "Unable to create Livox SDK2 config file");
    return 1;
  }

  LivoxLidarSdkVer version {};
  GetLivoxLidarSdkVer(&version);
  if (!LivoxLidarSdkInit(config_file.path().c_str())) {
    PrintError("sdk_init", "LivoxLidarSdkInit failed", SdkVersionText(version));
    return 3;
  }

  SetLivoxLidarInfoChangeCallback(LidarInfoChangeCallback, nullptr);
  const int sleep_ms = 100;
  const int loops = options.timeout_sec * 1000 / sleep_ms;
  for (int index = 0; index < loops && !g_match_found.load(); ++index) {
    std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
  }

  DeviceResult matched;
  DeviceResult mismatch;
  {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_match_found.load()) matched = g_matched_device;
    if (g_mismatch_found.load()) mismatch = g_mismatched_device;
  }

  LivoxLidarSdkUninit();
  if (matched.found) {
    PrintFound(matched, options, version);
    return 0;
  }

  PrintNotFound(options, mismatch.found ? &mismatch : nullptr, version);
  return 2;
}
