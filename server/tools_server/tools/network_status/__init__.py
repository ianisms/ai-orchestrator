from importlib import import_module

_module = import_module(".network_status", __name__)
register = _module.register

