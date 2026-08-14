#include "livox_lidar_api.h"
#include "livox_lidar_def.h"

#include <arpa/inet.h>
#include <signal.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>

namespace {

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
constexpr double kPointStaleMs = 2000.0;
constexpr double kImuStaleMs = 1500.0;
constexpr double kStartupGraceMs = 5000.0;

using SteadyClock = std::chrono::steady_clock;

struct Options {
  std::string host_ip;
  std::string expected_lidar_ip;
  std::string model;
  int metrics_interval_ms = 500;
};

struct RuntimeMetrics {
  std::mutex mutex;
  bool device_matched = false;
  std::uint32_t handle = 0;
  std::uint8_t dev_type = 0;
  std::string serial;
  std::string lidar_ip;

  std::uint64_t point_packets = 0;
  std::uint64_t imu_packets = 0;
  std::uint64_t point_total = 0;
  std::uint64_t lost_point_packets = 0;

  std::uint64_t interval_point_packets = 0;
  std::uint64_t interval_imu_packets = 0;
  std::uint64_t interval_points = 0;
  bool have_point_sequence = false;
  std::uint16_t last_point_sequence = 0;
  bool have_point_time = false;
  bool have_imu_time = false;
  SteadyClock::time_point last_point_time;
  SteadyClock::time_point last_imu_time;
  std::uint64_t lidar_timestamp = 0;
  std::uint8_t lidar_time_type = 0;
};

Options g_options;
RuntimeMetrics g_metrics;
std::atomic<bool> g_running(true);

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

bool ParseInterval(const std::string& value, int* interval_ms) {
  if (interval_ms == nullptr || value.empty()) return false;
  errno = 0;
  char* end = nullptr;
  const long parsed = std::strtol(value.c_str(), &end, 10);
  if (errno != 0 || end == value.c_str() || *end != '\0') return false;
  if (parsed < 200 || parsed > 5000) return false;
  *interval_ms = static_cast<int>(parsed);
  return true;
}

void PrintUsage() {
  std::cerr
      << "Usage:\n"
      << "  livox_stream --version\n"
      << "  livox_stream --host-ip <IPv4> --expected-lidar-ip <IPv4> "
         "--model <MID360|MID360S> [--metrics-interval-ms <200..5000>]\n";
}

bool ParseOptions(int argc, char* argv[], Options* options,
                  std::string* error) {
  bool host_set = false;
  bool lidar_set = false;
  bool model_set = false;
  for (int index = 1; index < argc; ++index) {
    const std::string option = argv[index];
    if (option != "--host-ip" && option != "--expected-lidar-ip" &&
        option != "--model" && option != "--metrics-interval-ms") {
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
      host_set = true;
    } else if (option == "--expected-lidar-ip") {
      options->expected_lidar_ip = value;
      lidar_set = true;
    } else if (option == "--model") {
      options->model = value;
      model_set = true;
    } else if (!ParseInterval(value, &options->metrics_interval_ms)) {
      *error = "Metrics interval must be 200..5000 milliseconds";
      return false;
    }
  }
  if (!host_set || !lidar_set || !model_set) {
    *error = "--host-ip, --expected-lidar-ip, and --model are required";
    return false;
  }
  if (!IsValidIpv4(options->host_ip) ||
      !IsValidIpv4(options->expected_lidar_ip)) {
    *error = "Host and expected LiDAR addresses must be valid IPv4";
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
  if (options.model == "MID360") return BuildMid360Config(options.host_ip);
  if (options.model == "MID360S") return BuildMid360SConfig(options.host_ip);
  return "";
}

std::uint64_t DecodeTimestamp(const std::uint8_t timestamp[8]) {
  std::uint64_t value = 0;
  for (int index = 0; index < 8; ++index) {
    value |= static_cast<std::uint64_t>(timestamp[index]) << (index * 8);
  }
  return value;
}

bool AcceptPacket(std::uint32_t handle, std::uint8_t dev_type) {
  return g_metrics.device_matched && g_metrics.handle == handle &&
         ModelFromDevType(dev_type) == g_options.model;
}

void LidarInfoChangeCallback(const std::uint32_t handle,
                             const LivoxLidarInfo* info, void*) {
  if (info == nullptr) return;
  const std::string model = ModelFromDevType(info->dev_type);
  if (model != g_options.model ||
      std::string(info->lidar_ip) != g_options.expected_lidar_ip) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_metrics.mutex);
  g_metrics.device_matched = true;
  g_metrics.handle = handle;
  g_metrics.dev_type = info->dev_type;
  g_metrics.serial = info->sn;
  g_metrics.lidar_ip = info->lidar_ip;
}

void PointCloudCallback(const std::uint32_t handle,
                        const std::uint8_t dev_type,
                        LivoxLidarEthernetPacket* data, void*) {
  if (data == nullptr) return;
  const auto now = SteadyClock::now();
  std::lock_guard<std::mutex> lock(g_metrics.mutex);
  if (!AcceptPacket(handle, dev_type)) return;

  ++g_metrics.point_packets;
  ++g_metrics.interval_point_packets;
  g_metrics.point_total += data->dot_num;
  g_metrics.interval_points += data->dot_num;
  if (g_metrics.have_point_sequence) {
    const std::uint16_t delta = static_cast<std::uint16_t>(
        data->udp_cnt - g_metrics.last_point_sequence);
    if (delta > 1 && delta < 32768) {
      g_metrics.lost_point_packets += static_cast<std::uint64_t>(delta - 1);
    }
  }
  g_metrics.have_point_sequence = true;
  g_metrics.last_point_sequence = data->udp_cnt;
  g_metrics.have_point_time = true;
  g_metrics.last_point_time = now;
  g_metrics.lidar_timestamp = DecodeTimestamp(data->timestamp);
  g_metrics.lidar_time_type = data->time_type;
}

void ImuDataCallback(const std::uint32_t handle,
                     const std::uint8_t dev_type,
                     LivoxLidarEthernetPacket* data, void*) {
  if (data == nullptr) return;
  const auto now = SteadyClock::now();
  std::lock_guard<std::mutex> lock(g_metrics.mutex);
  if (!AcceptPacket(handle, dev_type)) return;
  ++g_metrics.imu_packets;
  ++g_metrics.interval_imu_packets;
  g_metrics.have_imu_time = true;
  g_metrics.last_imu_time = now;
}

double AgeMilliseconds(SteadyClock::time_point now,
                       SteadyClock::time_point then) {
  return std::chrono::duration<double, std::milli>(now - then).count();
}

void PrintEvent(const std::string& event,
                const std::string& extra = "") {
  std::cout << "LIVOX_STREAM_EVENT={\"event\":\""
            << JsonEscape(event) << "\"" << extra << "}" << std::endl;
}

void PrintError(const std::string& error, const std::string& error_type) {
  PrintEvent("error", ",\"error_type\":\"" + JsonEscape(error_type) +
      "\",\"error\":\"" + JsonEscape(error) + "\"");
}

void PrintMetrics(SteadyClock::time_point started,
                  SteadyClock::time_point* previous_emit) {
  const auto now = SteadyClock::now();
  const double interval_sec =
      std::chrono::duration<double>(now - *previous_emit).count();
  const double uptime_sec =
      std::chrono::duration<double>(now - started).count();
  *previous_emit = now;

  std::uint64_t point_packets;
  std::uint64_t imu_packets;
  std::uint64_t point_total;
  std::uint64_t lost_packets;
  std::uint64_t interval_point_packets;
  std::uint64_t interval_imu_packets;
  std::uint64_t interval_points;
  bool have_sequence;
  bool have_point_time;
  bool have_imu_time;
  SteadyClock::time_point last_point_time;
  SteadyClock::time_point last_imu_time;
  std::uint64_t timestamp;
  std::uint8_t time_type;
  {
    std::lock_guard<std::mutex> lock(g_metrics.mutex);
    point_packets = g_metrics.point_packets;
    imu_packets = g_metrics.imu_packets;
    point_total = g_metrics.point_total;
    lost_packets = g_metrics.lost_point_packets;
    interval_point_packets = g_metrics.interval_point_packets;
    interval_imu_packets = g_metrics.interval_imu_packets;
    interval_points = g_metrics.interval_points;
    have_sequence = g_metrics.have_point_sequence && point_packets > 1;
    have_point_time = g_metrics.have_point_time;
    have_imu_time = g_metrics.have_imu_time;
    last_point_time = g_metrics.last_point_time;
    last_imu_time = g_metrics.last_imu_time;
    timestamp = g_metrics.lidar_timestamp;
    time_type = g_metrics.lidar_time_type;
    g_metrics.interval_point_packets = 0;
    g_metrics.interval_imu_packets = 0;
    g_metrics.interval_points = 0;
  }

  const double point_age_ms = have_point_time
      ? AgeMilliseconds(now, last_point_time) : -1.0;
  const double imu_age_ms = have_imu_time
      ? AgeMilliseconds(now, last_imu_time) : -1.0;
  std::string state = "STARTING";
  if (have_point_time && point_age_ms <= kPointStaleMs) {
    state = "STREAMING";
  } else if (uptime_sec * 1000.0 >= kStartupGraceMs) {
    state = "STALE";
  }
  std::string imu_status = "IDLE";
  if (have_imu_time) {
    imu_status = imu_age_ms <= kImuStaleMs ? "ACTIVE" : "STALE";
  }

  const double point_rate = interval_sec > 0.0
      ? interval_points / interval_sec : 0.0;
  const double point_packet_rate = interval_sec > 0.0
      ? interval_point_packets / interval_sec : 0.0;
  const double imu_rate = interval_sec > 0.0
      ? interval_imu_packets / interval_sec : 0.0;
  const double loss_percent = have_sequence
      ? 100.0 * static_cast<double>(lost_packets) /
          static_cast<double>(point_packets + lost_packets)
      : 0.0;

  // MID360S frame_cnt is not a logical frame sequence in the observed SDK
  // stream. Cloud Rate is therefore the measured point-packet rate, not a
  // synthetic 10 Hz value.
  std::cout << std::fixed << std::setprecision(2)
            << "LIVOX_STREAM_METRICS={"
            << "\"state\":\"" << state << "\","
            << "\"cloud_rate_hz\":" << point_packet_rate << ","
            << "\"point_count\":" << static_cast<std::uint64_t>(std::llround(point_rate)) << ","
            << "\"point_count_unit\":\"pts/s\","
            << "\"point_packet_rate_hz\":" << point_packet_rate << ","
            << "\"imu_status\":\"" << imu_status << "\","
            << "\"imu_rate_hz\":" << imu_rate << ","
            << "\"packet_loss_percent\":";
  if (have_sequence) std::cout << loss_percent;
  else std::cout << "null";
  std::cout << ",\"packet_loss_supported\":"
            << (have_sequence ? "true" : "false") << ","
            << "\"lidar_timestamp\":";
  if (have_point_time) std::cout << timestamp;
  else std::cout << "null";
  std::cout << ",\"lidar_time_type\":";
  if (have_point_time) std::cout << static_cast<unsigned int>(time_type);
  else std::cout << "null";
  std::cout << ",\"last_packet_age_ms\":";
  if (have_point_time) std::cout << std::max(0.0, point_age_ms);
  else std::cout << "null";
  std::cout << ",\"packet_counter\":" << point_packets + imu_packets << ","
            << "\"point_packet_counter\":" << point_packets << ","
            << "\"lost_point_packet_counter\":" << lost_packets << ","
            << "\"point_counter\":" << point_total << ","
            << "\"uptime_sec\":" << uptime_sec << "}"
            << std::endl;
}

void SignalHandler(int) {
  g_running.store(false);
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
  LivoxLidarSdkVer version {};
  GetLivoxLidarSdkVer(&version);
  if (argc == 2 && std::string(argv[1]) == "--version") {
    std::cout << "LIVOX_STREAM_EVENT={\"event\":\"version\","
              << "\"sdk_version\":\"" << SdkVersionText(version)
              << "\"}" << std::endl;
    return 0;
  }

  std::string input_error;
  if (!ParseOptions(argc, argv, &g_options, &input_error)) {
    PrintUsage();
    PrintError(input_error, "input");
    return 1;
  }
  const std::string config = BuildSdkConfig(g_options);
  if (config.empty()) {
    PrintError("Unsupported SDK configuration", "input");
    return 1;
  }
  const ScopedConfigFile config_file(
      "/tmp/cam_lidar_stream_" +
      std::to_string(static_cast<long long>(getpid())) + ".json");
  if (!config_file.Write(config)) {
    PrintError("Unable to write SDK configuration", "config");
    return 1;
  }

  DisableLivoxSdkConsoleLogger();
  if (!LivoxLidarSdkInit(config_file.path().c_str())) {
    PrintError("LivoxLidarSdkInit failed", "sdk_init");
    return 3;
  }
  SetLivoxLidarPointCloudCallBack(PointCloudCallback, nullptr);
  SetLivoxLidarImuDataCallback(ImuDataCallback, nullptr);
  SetLivoxLidarInfoChangeCallback(LidarInfoChangeCallback, nullptr);
  ::signal(SIGINT, SignalHandler);
  ::signal(SIGTERM, SignalHandler);

  PrintEvent("sdk_ready", ",\"sdk_version\":\"" +
      SdkVersionText(version) + "\",\"model\":\"" +
      JsonEscape(g_options.model) + "\"");
  const auto started = SteadyClock::now();
  auto previous_emit = started;
  while (g_running.load()) {
    std::this_thread::sleep_for(
        std::chrono::milliseconds(g_options.metrics_interval_ms));
    PrintMetrics(started, &previous_emit);
  }

  LivoxLidarSdkUninit();
  PrintEvent("sdk_uninitialized");
  return 0;
}
