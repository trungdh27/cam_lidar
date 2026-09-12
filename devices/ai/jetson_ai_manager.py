"""Bounded, read-mostly AI discovery and probe backend run through shared SSH."""

JETSON_AI_MANAGER = r'''
import json
import math
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

MARKER = "CAMERA_AI_JSON="
ROOT = Path("/tmp/cam_lidar_ai_sessions")
KEYWORDS = re.compile(r"(?:^|[/_ -])(ai|infer(?:ence)?|detect(?:ion)?|vision|yolo|deepstream|triton)(?:$|[/_ -])", re.I)
NODE_SIGNATURE = re.compile(r"infer(?:ence)?|detect(?:ion)?|vision|yolo|deepstream|triton", re.I)
INFERENCE_SIGNATURE = re.compile(r"tensorrt|triton|deepstream|onnxruntime|torch|ultralytics|yolo", re.I)
MODEL_SIGNATURE = re.compile(r"(?:--(?:model|engine|weights|config)(?:=|\s)|[^\s'\"]+\.(?:engine|onnx|pt|pth|plan))", re.I)
UTILITY_PROCESS_NAMES = {"tee", "grep", "bash", "sh", "python", "python3", "ssh", "scp", "cat", "sed", "find", "xargs"}
INPUT_WORDS = re.compile(r"image|camera|input|system_image|image_chunk", re.I)
OUTPUT_WORDS = re.compile(r"detect|infer|result|response|respond|object|classif|output", re.I)
SOFTWARE_ROOTS = ("/opt/vindynamics/controls",)
VALID_LAUNCH_METHODS = {"ros2_launch", "ros2_run", "executable", "systemd"}
SOURCE_INDICATORS = re.compile(r"inference|object_detection|segmentation|tracking|classification|vision|tensorrt|onnx|trained_models|detections|bounding.?box|confidence", re.I)
TOPIC_LITERAL = re.compile(r"['\"](/[^'\"\s]+)['\"]")
MODEL_LITERAL = re.compile(r"[^\s'\"]+\.(?:engine|plan|onnx|pt|pth)", re.I)

def emit(value):
    print(MARKER + json.dumps(value, separators=(",", ":"), default=str))

def run(command, env=None, timeout=6):
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as exc:
        return 127, "", str(exc)

def ros_environment():
    setup_files = []
    base = Path("/opt/ros")
    if base.exists():
        candidates = sorted(base.glob("*/setup.bash"))
        if candidates:
            setup_files.append(str(candidates[-1]))
    # Never recursively walk the whole home directory during a refresh.  ROS
    # workspaces conventionally sit one level below it; inherited ROS paths
    # still remain available if a workspace uses another layout.
    home = Path.home()
    setup_candidates = [home / "install" / "setup.bash"]
    try:
        setup_candidates.extend(item / "install" / "setup.bash" for item in sorted(home.iterdir())[:40] if item.is_dir())
    except Exception:
        pass
    for candidate in setup_candidates[:12]:
        if candidate.is_file():
            setup_files.append(str(candidate))
    env = os.environ.copy()
    if setup_files:
        code, output, error = run(["bash", "-lc", "; ".join(
            ["source " + shlex.quote(item) for item in setup_files] + ["env -0"]
        )], timeout=8)
        if code == 0:
            for item in output.split("\0"):
                if "=" in item:
                    key, value = item.split("=", 1); env[key] = value
    return env, setup_files

def ros_graph(env):
    code, nodes, node_error = run(["ros2", "node", "list"], env, 6)
    node_list = sorted(line.strip() for line in nodes.splitlines() if line.strip()) if code == 0 else []
    code, topics, topic_error = run(["ros2", "topic", "list", "-t"], env, 6)
    result = []
    if code == 0:
        for line in topics.splitlines():
            match = re.match(r"^(\S+)\s+\[(.+)\]$", line.strip())
            if match:
                result.append({"name": match.group(1), "message_type": match.group(2)})
    return node_list, result, [item for item in (node_error, topic_error) if item]

def runtime_info():
    python_executable = shutil.which("python3") or ("/usr/bin/python3" if Path("/usr/bin/python3").exists() else "python3")
    runtimes = []
    for name, module in (("TensorRT", "tensorrt"), ("PyTorch", "torch"), ("ONNX Runtime", "onnxruntime"), ("OpenCV", "cv2")):
        code, output, error = run([python_executable, "-c", "import " + module + "; print(getattr(" + module + ", '__version__', 'available'))"], timeout=5)
        runtimes.append({
            "name": name, "available": code == 0, "version": output if code == 0 else "",
            "python_executable": python_executable, "probe_return_code": code,
            "error": (error or output)[-1000:] if code else "",
        })
    return runtimes, python_executable

def process_candidates(configured=()):
    code, output, _ = run(["ps", "-eo", "pid=,ppid=,pgid=,comm=,args="], timeout=5)
    candidates = []
    ignored = {os.getpid()}
    parent = os.getppid()
    # Do not discover this probe or its SSH/launcher ancestry as an AI module.
    for _ in range(8):
        if not parent or parent in ignored:
            break
        ignored.add(parent)
        try:
            parent = int((Path("/proc") / str(parent) / "stat").read_text().split()[3])
        except Exception:
            break
    configured_names = set()
    for module in configured:
        if not isinstance(module, dict):
            continue
        if module.get("process_name"):
            configured_names.add(Path(str(module["process_name"])).name)
        command = ((module.get("launch_spec") or {}).get("command") or [])
        if command:
            configured_names.add(Path(str(command[0])).name)
    if code == 0:
        for line in output.splitlines():
            fields = line.strip().split(None, 4)
            if len(fields) < 4 or int(fields[0]) in ignored:
                continue
            process_name = Path(fields[3]).name
            arguments = fields[4] if len(fields) > 4 else ""
            # A filename containing "ai" is not process identity.  Utilities
            # are categorically never AI modules, even when logging an AI run.
            if process_name in UTILITY_PROCESS_NAMES:
                continue
            explicit = process_name in configured_names
            inference_with_model = bool(INFERENCE_SIGNATURE.search(arguments) and MODEL_SIGNATURE.search(arguments))
            if explicit or inference_with_model:
                candidates.append({"pid": int(fields[0]), "ppid": int(fields[1]), "process_group": int(fields[2]), "process_name": process_name, "arguments": arguments, "classification": "configured_executable" if explicit else "inference_runtime_with_model"})
    return candidates[:50]

def model_candidates(processes, env):
    found = []
    pattern = re.compile(r"[^\s'\"]+\.(?:engine|onnx|pt|pth|plan|yaml|yml|json)", re.I)
    for process in processes:
        for value in pattern.findall(process.get("arguments") or ""):
            if value not in found:
                found.append(value)
    # A deliberately opt-in config is the only source of an approved launch.
    config = Path.home() / ".config" / "cam_lidar" / "ai_modules.json"
    if config.exists():
        found.append(str(config))
    configuration = []
    # Targeted package config inspection only; configuration candidates never
    # create a module or imply that inference is active.
    for package in ("zed_wrapper", "zed_debug"):
        code, prefix, _ = run(["ros2", "pkg", "prefix", package], env, 4)
        if code != 0 or not prefix:
            continue
        for filename in ("object_detection.yaml", "custom_object_detection.yaml"):
            for candidate in (Path(prefix) / "share" / package / "config" / filename, Path(prefix) / "config" / filename):
                if candidate.is_file():
                    value = str(candidate)
                    if value not in found: found.append(value)
                    configuration.append({"path": value, "package": package, "kind": "ai_configuration_candidate", "active_module": False})
    return found[:30], configuration[:30]

def configured_modules():
    path = Path.home() / ".config" / "cam_lidar" / "ai_modules.json"
    try:
        raw = json.loads(path.read_text()) if path.exists() else []
        return raw if isinstance(raw, list) else []
    except Exception:
        return []

def bounded_files(root, names, limit=120):
    if not Path(root).is_dir():
        return []
    command = ["find", root, "-maxdepth", "6", "-type", "f", "("]
    for index, name in enumerate(names):
        if index: command.append("-o")
        command.extend(["-name", name])
    command.extend([")", "-print"])
    code, output, _ = run(command, timeout=8)
    return [line for line in output.splitlines() if line][:limit] if code == 0 else []

def source_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:200000]
    except Exception:
        return ""

def package_for_path(path, roots):
    current = Path(path).parent
    for _ in range(7):
        manifest = current / "package.xml"
        if manifest.is_file():
            match = re.search(r"<name>\s*([^<\s]+)\s*</name>", source_text(manifest))
            return match.group(1) if match else current.name
        if str(current) in roots or current.parent == current:
            break
        current = current.parent
    return ""

def installed_software_candidates(env, package_names=()):
    """Inspect only approved robot roots and bounded ROS package shares.

    A package/share hit is evidence, not an active module.  It is deliberately
    kept separate from the explicit registry below so an installed model or a
    YAML file cannot accidentally become a launchable module.
    """
    roots = [root for root in SOFTWARE_ROOTS if Path(root).is_dir()]
    candidate_packages = [name for name in package_names if name.startswith("vd_") or SOURCE_INDICATORS.search(name)][:20]
    package_details = []
    for package in candidate_packages:
        code, prefix, _ = run(["ros2", "pkg", "prefix", package], env, 4)
        if code != 0 or not prefix or not Path(prefix).is_dir():
            continue
        share = Path(prefix) / "share" / package
        root = str(share if share.is_dir() else Path(prefix))
        if root not in roots:
            roots.append(root)
        code, executables, _ = run(["ros2", "pkg", "executables", package], env, 4)
        package_details.append({"package": package, "prefix": prefix, "executables": executables.splitlines()[:30] if code == 0 else []})
    launch_files = []
    config_files = []
    model_files = []
    candidates = []
    for root in roots:
        launch_files.extend(bounded_files(root, ("*.launch.py", "*.launch.xml", "*.launch.yaml"), 80))
        config_files.extend(bounded_files(root, ("*.yaml", "*.yml", "*.json"), 120))
        model_files.extend(bounded_files(root, ("*.engine", "*.plan", "*.onnx", "*.pt", "*.pth"), 80))
    for path in launch_files + config_files:
        text = source_text(path)
        if not SOURCE_INDICATORS.search(text):
            continue
        models = sorted(set(MODEL_LITERAL.findall(text)))[:10]
        topics = sorted(set(TOPIC_LITERAL.findall(text)))[:20]
        package = package_for_path(path, roots)
        evidence = ["source=" + path]
        if package: evidence.append("package=" + package)
        if models: evidence.append("model_reference")
        if topics: evidence.append("endpoint_reference")
        candidates.append({
            "candidate_uid": "source-" + re.sub(r"[^a-zA-Z0-9_-]", "-", path).strip("-")[-100:],
            "display_name": Path(path).stem, "confidence": "STRONG_CANDIDATE" if models and topics else "WEAK_CANDIDATE",
            "source_path": path, "package": package, "launch_file": path if ".launch." in path else "",
            "model_references": models, "endpoint_references": topics,
            "evidence": evidence, "registration_required": True,
        })
    return {
        "roots": roots, "launch_files": sorted(set(launch_files))[:80],
        "configuration_files": sorted(set(config_files))[:120], "model_files": sorted(set(model_files))[:80],
        "module_candidates": candidates[:80], "package_details": package_details,
    }

def explicit_registered_modules(configured):
    modules, candidates = [], []
    for raw in configured:
        if not isinstance(raw, dict) or not raw.get("module_uid"):
            continue
        launch = raw.get("launch_spec") or {}
        # The first token of a ROS launch command is usually `ros2`, not the
        # AI executable.  Require an explicit executable/process identity so
        # a guessed launch command cannot turn into a registered module.
        executable = raw.get("executable") or raw.get("process_name") or launch.get("executable")
        model = raw.get("model_path") or raw.get("model_name")
        inputs, outputs = raw.get("input_topics") or [], raw.get("output_topics") or []
        static_complete = bool(raw.get("package") and executable and model and inputs and outputs)
        command = launch.get("command") or []
        launch_method = str(launch.get("method") or "")
        command_safe = isinstance(command, list) and all(isinstance(item, str) and item for item in command)
        if not command:
            safe_launch = True
        elif not command_safe or launch_method not in VALID_LAUNCH_METHODS:
            safe_launch = False
        elif launch_method == "ros2_launch":
            safe_launch = len(command) >= 4 and command[:3] == ["ros2", "launch", str(raw.get("package"))] and command[3] == str(launch.get("launch_file") or "")
        elif launch_method == "ros2_run":
            safe_launch = len(command) >= 4 and command[:3] == ["ros2", "run", str(raw.get("package"))] and command[3] == str(executable)
        elif launch_method == "executable":
            safe_launch = Path(command[0]).name == Path(str(executable)).name
        else:  # systemd: a declarative service name, never an arbitrary shell command.
            safe_launch = len(command) == 3 and command[:2] == ["systemctl", "start"] and bool(launch.get("service")) and command[2] == str(launch.get("service"))
        if not static_complete or not safe_launch:
            candidates.append({
                "candidate_uid": "registry-" + str(raw["module_uid"]), "display_name": raw.get("display_name") or raw["module_uid"],
                "confidence": "STRONG_CANDIDATE", "source_path": str(Path.home() / ".config" / "cam_lidar" / "ai_modules.json"),
                "evidence": ["explicit registry entry", "incomplete static module metadata"], "registration_required": True,
            })
            continue
        item = dict(raw); metadata = dict(item.get("metadata") or {})
        launch_ready = bool(launch.get("approved") and command and safe_launch)
        normalized_launch = dict(launch)
        normalized_launch["launch_ready"] = launch_ready
        item["launch_spec"] = normalized_launch
        metadata.update({
            "confidence": "CONFIRMED", "discovery_sources": ["explicit_ai_modules_registry"],
            "configuration_state": "CONFIGURED", "launch_ready": launch_ready,
            "execution_host": raw.get("execution_host") or "Jetson",
            "framework": raw.get("framework") or raw.get("runtime") or "UNKNOWN",
        })
        item["metadata"] = metadata
        item["status"] = "READY_TO_LAUNCH" if metadata["launch_ready"] else "CONFIGURED"
        modules.append(item)
    return modules, candidates

def endpoint(topic, role):
    return {"name": topic.get("name", ""), "message_type": topic.get("message_type", ""), "role": role, "metadata": {"discovery": "ros_graph_heuristic"}}

def discover():
    env, setup_files = ros_environment()
    ros2_available = run(["bash", "-lc", "command -v ros2"], env, 3)[0] == 0
    nodes, topics, graph_errors = ros_graph(env) if ros2_available else ([], [], [])
    code, packages, _ = run(["ros2", "pkg", "list"], env, 6) if ros2_available else (1, "", "")
    ai_packages = sorted(item for item in packages.splitlines() if KEYWORDS.search(item)) if code == 0 else []
    configured = configured_modules()
    registered_configured, registry_candidates = explicit_registered_modules(configured)
    package_names = packages.splitlines() if code == 0 else []
    software = installed_software_candidates(env, package_names)
    processes = process_candidates(configured)
    runtimes, python_executable = runtime_info()
    available_runtimes = [item for item in runtimes if item["available"] and item["name"] in ("TensorRT", "PyTorch", "ONNX Runtime")]
    inputs = [endpoint(item, "input") for item in topics if INPUT_WORDS.search(item["name"])]
    outputs = [endpoint(item, "output") for item in topics if OUTPUT_WORDS.search(item["name"])]
    anchors = [node for node in nodes if NODE_SIGNATURE.search(node)]
    modules = []
    for index, item in enumerate(registered_configured):
        current = dict(item); metadata = dict(current.get("metadata") or {})
        process_name = Path(str(current.get("process_name") or current.get("executable") or ((current.get("launch_spec") or {}).get("command") or [""])[0])).name
        matched = next((process for process in processes if process["process_name"] == process_name), None)
        if matched:
            current["status"] = "RUNNING"; metadata["running_evidence"] = {"process_name": process_name, "classification": matched["classification"]}
        current["metadata"] = metadata; modules.append(current)
    dynamic_candidates = [
        {"candidate_uid": "process-" + str(item["pid"]), "display_name": item["process_name"], "confidence": "STRONG_CANDIDATE", "evidence": [item["classification"], "running process"], "registration_required": True}
        for item in processes if not any(Path(str(module.get("process_name") or "")).name == item["process_name"] for module in modules)
    ] + [
        {"candidate_uid": "node-" + re.sub(r"[^a-zA-Z0-9_-]", "-", node).strip("-")[:80], "display_name": node, "confidence": "WEAK_CANDIDATE", "evidence": ["AI-like ROS node name without static package/config linkage"], "registration_required": True}
        for node in anchors if not any(module.get("ros_node") == node for module in modules)
    ]
    models, configurations = model_candidates(processes, env)
    for path in software["model_files"]:
        if path not in models: models.append(path)
    for path in software["configuration_files"]:
        if path.endswith((".yaml", ".yml")) and SOURCE_INDICATORS.search(source_text(path)):
            configurations.append({"path": path, "package": package_for_path(path, software["roots"]), "kind": "robot_software_configuration_candidate", "active_module": False})
    module_running = any(item.get("status") in ("EXTERNAL", "RUNNING") for item in modules)
    warnings = []
    if not modules:
        warnings.append("No AI module could be identified from approved configuration, AI-named ROS nodes, or AI-related processes.")
    return {
        "runtime_available": bool(available_runtimes), "runtime_name": available_runtimes[0]["name"] if available_runtimes else "UNKNOWN",
        "runtime_version": available_runtimes[0]["version"] if available_runtimes else "", "runtimes": runtimes, "runtime_probe_results": runtimes,
        "ros_available": ros2_available, "ros_distro": env.get("ROS_DISTRO", ""), "setup_files": setup_files,
        "ai_packages": ai_packages[:80], "candidate_processes": processes, "candidate_nodes": anchors,
        "input_topics": inputs, "output_topics": outputs, "model_candidates": models, "configuration_candidates": configurations,
        "module_candidates": (registry_candidates + dynamic_candidates + software["module_candidates"])[:80], "registration_required": bool((registry_candidates or dynamic_candidates or software["module_candidates"]) and not modules),
        "robot_software_roots": software["roots"], "installed_packages": software["package_details"],
        "launch_files": software["launch_files"], "installed_configuration_files": software["configuration_files"],
        "launch_candidates": [item.get("launch_spec") for item in modules if item.get("launch_spec")],
        "modules": modules, "warnings": warnings, "errors": graph_errors,
        "module_count": len(modules), "module_discovered": bool(modules), "module_running": module_running,
        "execution_host": "Jetson", "execution_user": os.environ.get("USER") or os.environ.get("LOGNAME") or "UNKNOWN", "hostname": os.uname().nodename,
        "python_executable": python_executable, "python_version": sys.version.split()[0], "path": os.environ.get("PATH", "")[-4096:],
        "python_search_path": sys.path[:30], "gpu_available": Path("/usr/bin/tegrastats").exists(),
    }

def valid_identity(metadata, request):
    return bool(metadata and metadata.get("owned_by_test") and all(
        str(metadata.get(key)) == str(request.get(key))
        for key in ("session_id", "module_uid", "pid", "process_group")
    ))

def pg_members(metadata):
    pgid = metadata.get("process_group") if metadata else None
    if not pgid:
        return []
    code, output, _ = run(["ps", "-eo", "pid=,pgid="], timeout=4)
    return [int(line.split()[0]) for line in output.splitlines() if line.split() and len(line.split()) > 1 and line.split()[1] == str(pgid)] if code == 0 else []

def action_start(request):
    module = request.get("module") or {}
    launch = module.get("launch_spec") or {}
    command = launch.get("command") or []
    if not launch.get("approved") or not command or not all(isinstance(item, str) for item in command):
        return {"ok": False, "error_type": "AI_LAUNCH_FAILED", "error": "No approved AI launch specification is available."}
    env, setup_files = ros_environment()
    ROOT.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex; runtime = ROOT / session_id; runtime.mkdir()
    log_path = runtime / "launch.log"
    with log_path.open("wb") as stream:
        process = subprocess.Popen(command, cwd=None, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    metadata = {"session_id": session_id, "module_uid": module.get("module_uid"), "pid": process.pid, "process_group": process.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "log_path": str(log_path), "owned_by_test": True,
                "launch_command_summary": " ".join(shlex.quote(item) for item in command), "expected_node": launch.get("expected_node", ""), "setup_files": setup_files}
    (runtime / "session.json").write_text(json.dumps(metadata))
    return {"ok": True, "session": metadata}

def session_status(request):
    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", request.get("session_id") or "")
    metadata_path = ROOT / session_id / "session.json"; metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    if not valid_identity(metadata, request):
        return {"ok": False, "error_type": "AI_SESSION_IDENTITY_MISMATCH", "error": "Exact test-owned AI session identity does not match."}
    env, _ = ros_environment(); node = metadata.get("expected_node")
    nodes, _, _ = ros_graph(env) if node else ([], [], [])
    return {"ok": True, "status": {"process_alive": bool(pg_members(metadata)), "expected_node_alive": bool(node and node in nodes), "node_names": nodes, "stderr_summary": (Path(metadata["log_path"]).read_text(errors="replace")[-8192:] if Path(metadata["log_path"]).exists() else "")}}

def action_stop(request):
    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", request.get("session_id") or "")
    runtime = ROOT / session_id; path = runtime / "session.json"; metadata = json.loads(path.read_text()) if path.exists() else {}
    if not valid_identity(metadata, request):
        return {"ok": False, "error_type": "AI_SESSION_IDENTITY_MISMATCH", "error": "Exact test-owned AI session identity does not match."}
    members = pg_members(metadata)
    if members:
        try: os.killpg(int(metadata["process_group"]), signal.SIGTERM)
        except ProcessLookupError: pass
    deadline = time.monotonic() + min(8, float(request.get("timeout_s") or 8))
    while pg_members(metadata) and time.monotonic() < deadline: time.sleep(.1)
    remaining = pg_members(metadata)
    if not remaining:
        for item in runtime.iterdir(): item.unlink()
        runtime.rmdir()
    return {"ok": not bool(remaining), "stopped": not bool(remaining), "remaining_pids": remaining,
            "error_type": "AI_CLEANUP_FAILED" if remaining else None, "error": "Owned process remains" if remaining else None}

def probe(request):
    endpoint = request.get("endpoint") or {}; topic = endpoint.get("name", ""); expected = endpoint.get("message_type", "")
    env, _ = ros_environment()
    code, output, error = run([sys.executable, "-c", r"""
import json, math, sys, time
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rosidl_runtime_py.utilities import get_message
from rclpy.serialization import serialize_message, deserialize_message
q=json.loads(sys.argv[1]); rclpy.init(args=None); n=rclpy.create_node("cam_lidar_ai_probe_" + str(__import__('os').getpid()))
try:
 types=dict(n.get_topic_names_and_types()); actual=(types.get(q['topic']) or [''])[0]; pub=len(n.get_publishers_info_by_topic(q['topic']))
 result={'endpoint_exists':bool(actual),'message_type':actual,'type_matches':not q.get('expected') or actual==q.get('expected'),'publisher_count':pub,'sample_count':0,'valid_sample_count':0,'timestamps':[],'timestamp_rollback_count':0,'deserialize_error_count':0,'structural_error_count':0,'image_metadata':{},'optional_summary':{}}
 if not actual: print(json.dumps(result)); raise SystemExit
 cls=get_message(actual); last=[None]; received=[]
 def cb(message):
  result['sample_count']+=1
  try:
   deserialize_message(serialize_message(message), cls)
  except Exception:
   result['deserialize_error_count']+=1; return
  fields=getattr(message, 'get_fields_and_field_types', lambda: {})()
  if not fields: result['structural_error_count']+=1
  for field_name in fields:
   value=getattr(message, field_name, None)
   if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value): result['structural_error_count']+=1
  stamp=None; header=getattr(message,'header',None)
  if header is not None and hasattr(header,'stamp'):
   stamp=header.stamp.sec + header.stamp.nanosec/1e9
   if last[0] is not None and stamp < last[0]: result['timestamp_rollback_count']+=1
   last[0]=stamp; result['timestamps'].append(stamp)
  if hasattr(message,'width'): result['image_metadata']['width']=message.width
  if hasattr(message,'height'): result['image_metadata']['height']=message.height
  if hasattr(message,'encoding'): result['image_metadata']['encoding']=message.encoding
  if header is not None and hasattr(header,'frame_id'): result['image_metadata']['frame_id']=header.frame_id
  result['valid_sample_count']+=1; received.append(time.monotonic())
 qos=QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST)
 sub=n.create_subscription(cls,q['topic'],cb,qos); deadline=time.monotonic()+q['timeout']
 while time.monotonic()<deadline and result['valid_sample_count']<q['samples']: rclpy.spin_once(n,timeout_sec=.1)
 result['observed_rate_hz']=round((len(received)-1)/(received[-1]-received[0]),3) if len(received)>1 and received[-1]>received[0] else 0.0
 result['first_timestamp']=result['timestamps'][0] if result['timestamps'] else None; result['last_timestamp']=result['timestamps'][-1] if result['timestamps'] else None
 result['structurally_valid']=result['valid_sample_count']>0 and result['deserialize_error_count']==0 and result['structural_error_count']==0
 print(json.dumps(result))
finally: n.destroy_node(); rclpy.shutdown()
""", json.dumps({'topic':topic,'expected':expected,'timeout':float(request.get('timeout_s') or 8),'samples':int(request.get('sample_count') or 1)})], env, float(request.get('timeout_s') or 8)+5)
    if code != 0: return {"ok": False, "error_type": "AI_ENDPOINT_PROBE_FAILED", "error": (error or output)[-2000:]}
    try: data=json.loads(output.splitlines()[-1])
    except Exception: return {"ok": False, "error_type": "AI_ENDPOINT_PROBE_FAILED", "error": output[-2000:]}
    return {"ok": True, "probe": data}

request=json.loads(sys.argv[1]); action=request.get("action")
if action == 'discover': emit({'ok':True,'environment':discover()})
elif action == 'start': emit(action_start(request))
elif action == 'status': emit(session_status(request))
elif action == 'stop': emit(action_stop(request))
elif action == 'probe': emit(probe(request))
else: emit({'ok':False,'error_type':'AI_PROBE_ERROR','error':'Unknown action'})
'''
