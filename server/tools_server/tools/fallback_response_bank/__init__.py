from importlib import import_module

_module = import_module(".fallback_response_bank", __name__)
register = _module.register

