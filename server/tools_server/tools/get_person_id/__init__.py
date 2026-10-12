from importlib import import_module

_module = import_module(".get_person_id", __name__)
register = _module.register
