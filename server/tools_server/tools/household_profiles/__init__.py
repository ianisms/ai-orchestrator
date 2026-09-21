from importlib import import_module

_module = import_module(".household_profiles", __name__)
register = _module.register

