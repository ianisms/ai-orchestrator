from importlib import import_module

_module = import_module(".household_memory", __name__)
register = _module.register

