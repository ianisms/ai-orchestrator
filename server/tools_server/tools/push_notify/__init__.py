from importlib import import_module

_module = import_module(".push_notify", __name__)
register = _module.register
