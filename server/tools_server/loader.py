from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable

from watchfiles import awatch


class ToolLoader:
    def __init__(
        self,
        server,
        tools_dir: Path,
        logger,
        notify_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._server = server
        self._tools_dir = tools_dir
        self._log = logger
        self._notify_changed = notify_changed
        self._modules: dict[Path, ModuleType] = {}
        self._tool_names: dict[Path, list[str]] = {}
        self._lock = asyncio.Lock()

        root = str(self._tools_dir.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        if "tools" not in sys.modules:
            pkg = ModuleType("tools")
            pkg.__path__ = [str(self._tools_dir)]
            sys.modules["tools"] = pkg

    def _iter_tool_files(self) -> list[Path]:
        if not self._tools_dir.exists():
            return []
        files: list[Path] = []
        for path in sorted(self._tools_dir.iterdir()):
            if path.name.startswith("_"):
                continue
            if path.is_file() and path.suffix == ".py":
                if path.name == "__init__.py":
                    continue
                files.append(path)
                continue
            if path.is_dir():
                init_path = path / "__init__.py"
                if init_path.exists():
                    files.append(init_path)
        return files

    def _module_name(self, path: Path) -> str:
        if path.name == "__init__.py":
            return f"tools.{path.parent.name}"
        return f"tools.{path.stem}"

    def _module_file_for_change(self, path: Path) -> Path | None:
        if path.name.startswith("."):
            return None
        if path.name == "__init__.py" and path.parent == self._tools_dir:
            return None
        if path.is_file() and path.suffix == ".py" and path.parent == self._tools_dir:
            return path
        if path.name == "__init__.py" and path.parent.parent == self._tools_dir:
            return path
        if path.parent.parent == self._tools_dir:
            init_path = path.parent / "__init__.py"
            if init_path.exists():
                return init_path
        return None

    async def load_all(self) -> None:
        async with self._lock:
            changed = False
            for path in self._iter_tool_files():
                if await self._load_module(path):
                    changed = True
            if changed:
                await self._maybe_notify()

    async def watch(self) -> None:
        if not self._tools_dir.exists():
            self._log.warning("tools dir missing: %s", self._tools_dir)
            return
        async for changes in awatch(self._tools_dir):
            paths = {Path(path) for _, path in changes}
            if not paths:
                continue
            module_paths: set[Path] = set()
            for path in paths:
                module_path = self._module_file_for_change(path)
                if module_path is not None:
                    module_paths.add(module_path)
            if module_paths:
                await self.reload_from_paths(module_paths)

    async def reload_from_paths(self, paths: set[Path]) -> None:
        async with self._lock:
            changed = False
            for path in paths:
                if path.suffix != ".py":
                    continue
                if not path.exists():
                    if await self._remove_module(path):
                        changed = True
                    continue
                if await self._load_module(path):
                    changed = True
            if changed:
                await self._maybe_notify()

    async def _load_module(self, path: Path) -> bool:
        module_name = self._module_name(path)
        existing = self._modules.get(path)
        if existing is not None:
            self._unregister_tools(path)
            try:
                module = importlib.reload(existing)
            except Exception as exc:
                self._log.warning("tool reload failed: %s error=%r", path.name, exc)
                return False
        else:
            if path.name == "__init__.py":
                spec = importlib.util.spec_from_file_location(
                    module_name, path, submodule_search_locations=[str(path.parent)]
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                self._log.warning("tool import skipped: %s", path.name)
                return False
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                self._log.warning("tool import failed: %s error=%r", path.name, exc)
                return False

        tool_names = await self._register_module(module)
        self._modules[path] = module
        self._tool_names[path] = tool_names
        if tool_names:
            self._log.info("tool loaded: %s tools=%s", path.name, ",".join(tool_names))
        else:
            self._log.info("tool loaded: %s tools=<none>", path.name)
        return True

    async def _register_module(self, module: ModuleType) -> list[str]:
        register = getattr(module, "register", None)
        if register is None or not callable(register):
            self._log.warning("tool module missing register(): %s", module.__name__)
            return []
        try:
            if inspect.iscoroutinefunction(register):
                result = await register(self._server)
            else:
                result = register(self._server)
        except Exception as exc:
            self._log.warning("tool register failed: %s error=%r", module.__name__, exc)
            return []
        if result is None:
            return []
        if isinstance(result, str):
            return [result]
        if isinstance(result, list):
            return [str(name) for name in result if str(name).strip()]
        return []

    async def _remove_module(self, path: Path) -> bool:
        if path not in self._modules:
            return False
        self._unregister_tools(path)
        module = self._modules.pop(path, None)
        self._tool_names.pop(path, None)
        if module is not None:
            self._log.info("tool removed: %s", path.name)
        return True

    def _unregister_tools(self, path: Path) -> None:
        names = self._tool_names.get(path, [])
        for name in names:
            try:
                self._server.remove_tool(name)
            except Exception as exc:
                self._log.warning("tool remove failed: %s error=%r", name, exc)

    async def _maybe_notify(self) -> None:
        if self._notify_changed is None:
            return
        try:
            await self._notify_changed()
        except Exception as exc:
            self._log.warning("tool change notify failed: %r", exc)
