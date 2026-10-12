from importlib import import_module

_module = import_module(".location_preferences", __name__)
register = _module.register

