from importlib import import_module

_module = import_module(".speaker_enrollment_coach", __name__)
register = _module.register

