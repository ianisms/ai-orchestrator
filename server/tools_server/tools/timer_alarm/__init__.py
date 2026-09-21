from importlib import import_module

_module = import_module(".timer_alarm", __name__)
register = _module.register

