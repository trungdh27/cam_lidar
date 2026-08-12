class DeviceRegistry:

    def __init__(self):
        self._devices = {}

    def register(self, name, device):
        self._devices[name] = device

    def unregister(self, name):
        self._devices.pop(name, None)

    def get(self, name):
        return self._devices.get(name)

    def all(self):
        return dict(self._devices)

    def names(self):
        return list(self._devices.keys())
