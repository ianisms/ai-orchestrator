from importlib import import_module

_module = import_module(".audio_quality_guard", __name__)
register = _module.register

