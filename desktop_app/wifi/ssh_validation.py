"""Short-lived Wi-Fi qualification using the Dashboard's real SSH settings."""
from __future__ import annotations

import asyncio
import re

import asyncssh

IDENTITY_COMMAND = "hostnamectl --static 2>/dev/null || hostname"
VALIDATION_COMMAND = "printf 'WIFI_SSH_OK\\n'; " + IDENTITY_COMMAND


async def probe_tcp(host, port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    writer.close()
    await writer.wait_closed()


async def validate_wifi_ssh(ssh, setup, target, *, local, notify):
    """Never change the shared manager/configuration; close only our owned session.

    AsyncSSH's effective options preserve alias-resolved keys, password providers,
    username, port and host-key policy. A proxy/tunnel must not qualify a direct
    Wi-Fi path, so those transports are explicitly removed on the temporary copy.
    """
    values = {"SSH target": target, "SSH validation method": "NOT STARTED"}
    failures = {}

    def failure(metric, status, reason):
        failures[metric] = {"status": status, "reason": reason}
        values[metric] = False
        notify(level="ERROR", message=reason)

    def done():
        values["SSH criterion errors"] = failures
        values["SSH reachable"] = bool(values.get("SSH authentication") and values.get("Remote command execution"))
        return values

    stdout, _, code = await local(f"ip route get {target}", 5)
    route = re.search(r"\bdev\s+(\S+)", stdout)
    values["Route interface"] = route.group(1) if route else "UNKNOWN"
    if code or values["Route interface"] != setup.client_wifi_interface:
        failure("SSH authentication", "ERROR", "SSH CONFIG ERROR: qualification route does not use the client Wi-Fi interface")
        return done()
    notify(level="INFO", message=f"Wi-Fi route verified through {values['Route interface']}")
    connection = getattr(ssh, "_connection", None)
    options = getattr(connection, "_options", None)
    config = getattr(ssh, "config", None)
    port = getattr(options, "port", None) or getattr(config, "port", 22)
    values["SSH port"] = port
    try:
        await probe_tcp(target, port)
        values["TCP22 reachable"] = True
        notify(level="INFO", message=f"TCP/{port} reachable at {target}")
    except PermissionError:
        failure("TCP22 reachable", "ERROR", "SSH CONFIG ERROR: local TCP probe permission denied")
        return done()
    except (OSError, asyncio.TimeoutError):
        failure("TCP22 reachable", "FAIL", f"SSH PORT UNREACHABLE: TCP/{port} unreachable or timed out")
        return done()
    try:
        identity = await ssh.run(IDENTITY_COMMAND, timeout=5)
        expected = identity.stdout.strip()
        if identity.exit_status or not expected:
            raise RuntimeError("identity unavailable")
        values["Expected host"] = expected
    except Exception:
        failure("Host identity valid", "ERROR", "REMOTE COMMAND FAILED: current shared Jetson host identity unavailable")
        return done()
    peer = connection.get_extra_info("peername") if connection is not None else None
    peer_ip = peer[0] if peer else None
    values["Shared SSH remote IP"] = peer_ip or "UNKNOWN"
    temporary = None
    notify(level="INFO", message="Starting SSH validation")
    try:
        if peer_ip == target and not getattr(options, "tunnel", None) and not getattr(options, "proxy_command", None):
            values["SSH validation method"] = "SHARED SESSION"
            session = ssh
        else:
            values["SSH validation method"] = "SHORT-LIVED DIRECT WIFI"
            if options is not None:
                effective = {name: getattr(options, name) for name in (
                    "username", "port", "password", "client_keys", "agent_path",
                    "agent_identities", "known_hosts", "host_key_alias", "client_factory",
                    "public_key_auth", "password_auth", "kbdint_auth", "preferred_auth")
                    if hasattr(options, name)}
                # Do not pass the old options object to the constructor: it
                # retains loaded HostName/ProxyJump alias rules in last_config.
                settings = {key: value for key, value in options.kwargs.items() if key != "last_config"}
                settings.update(effective)
                settings.update(host=target, config=None, tunnel=None, proxy_command=None)
                kwargs = {"options": asyncssh.SSHClientConnectionOptions(**settings)}
            elif config is not None and getattr(config, "username", None):
                # Exact fallback used by SSHManager.connect(), not guessed credentials.
                kwargs = {"username": config.username, "port": config.port}
                if config.password:
                    kwargs["password"] = config.password
            else:
                failure("SSH authentication", "ERROR", "SSH CONFIG ERROR: no reusable application authentication configuration")
                return done()
            temporary = await asyncio.wait_for(asyncssh.connect(
                target, local_addr=(setup.laptop_wifi_ip, 0), **kwargs), 10)
            session = temporary
        values["SSH authentication"] = True
        notify(level="INFO", message="SSH authentication succeeded")
        command = await session.run(VALIDATION_COMMAND, timeout=5)
        lines = command.stdout.strip().splitlines()
        values["Remote command execution"] = command.exit_status == 0 and bool(lines) and lines[0] == "WIFI_SSH_OK"
        if not values["Remote command execution"]:
            failure("Remote command execution", "FAIL", "REMOTE COMMAND FAILED: login succeeded but WIFI_SSH_OK was not returned")
        else:
            actual = "\n".join(lines[1:]).strip()
            values["Actual host"] = actual
            values["Host identity valid"] = actual == expected
            if not values["Host identity valid"]:
                failure("Host identity valid", "FAIL", f"HOST IDENTITY MISMATCH: Expected: {expected}; Actual: {actual}")
    except asyncssh.PermissionDenied:
        failure("SSH authentication", "FAIL", "SSH AUTH FAILED: password/public-key authentication failed")
    except asyncssh.HostKeyNotVerifiable:
        failure("SSH authentication", "ERROR", "HOST KEY ERROR: host verification failed")
    except (OSError, asyncio.TimeoutError, asyncssh.Error):
        metric = "Remote command execution" if values.get("SSH authentication") else "SSH authentication"
        failure(metric, "ERROR", "REMOTE COMMAND FAILED: SSH transport or command timed out/failed")
    except Exception:
        failure("SSH authentication", "ERROR", "SSH CONFIG ERROR: application authentication configuration could not be reused")
    finally:
        if temporary is not None:
            temporary.close()
            await temporary.wait_closed()
    return done()
