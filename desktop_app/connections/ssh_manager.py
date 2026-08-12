import paramiko


class SSHManager:
    def __init__(self):
        self.client = None

    def connect(self, host, username, password=None, port=22, timeout=8):
        self.disconnect()

        client = paramiko.SSHClient()

        # Safer than AutoAddPolicy:
        # first connect once using the normal `ssh` command so the host key
        # is stored in ~/.ssh/known_hosts.
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=True,
            look_for_keys=True,
        )

        self.client = client
        return True

    def execute(self, command, timeout=10):
        if self.client is None:
            raise RuntimeError("SSH is not connected.")

        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        return {
            "command": command,
            "exit_code": exit_code,
            "stdout": out,
            "stderr": err,
        }

    def disconnect(self):
        if self.client is not None:
            self.client.close()
            self.client = None
