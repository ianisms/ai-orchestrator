from importlib import import_module

_module = import_module(".server_health", __name__)
register = _module.register
