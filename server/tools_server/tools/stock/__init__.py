from importlib import import_module, reload

_module = import_module(".stock", __name__)
try:
    _module = reload(_module)
except Exception:
    pass

register = _module.register

__all__ = ["register"]
