from importlib import import_module

_module = import_module(".speaker_identity", __name__)
register = _module.register

