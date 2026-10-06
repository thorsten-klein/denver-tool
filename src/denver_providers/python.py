"""python provider: call a function from a Python file, in denver's own process.

Keys: ``file:``, ``function:``, optional ``args:``. See ``doc/providers/python.md``.
"""

import importlib.util
import re
import subprocess
import sys
import traceback

from denver_errors import DenverError

from .base import Provider
from .context import banner, die, info

# one module per file: stages using the same file share it.
MODULE_PREFIX = "denver_python_stage_"


def _module_name(path):
    """A module name unique per file."""
    return MODULE_PREFIX + re.sub(r"\W+", "_", str(path.with_suffix("")).strip("/"))


class PythonProvider(Provider):
    """Imports 'file:' and calls 'function:' as function(ctx, **args)."""

    name = "python"
    KEYS = ("file", "function", "args")

    def _validate_cfg(self, file, function, args):
        """Die on bad 'file:'/'function:'/'args:'."""
        for key, value in (("file", file), ("function", function)):
            if not isinstance(value, str) or not value.strip():
                die(f"python[{self.stage}]: '{key}' must be a non-empty string")
        if args is not None and not (isinstance(args, dict) and all(isinstance(k, str) for k in args)):
            die(f"python[{self.stage}]: 'args' must be a mapping of keyword argument names to values")

    def _load_module(self, path):
        """Import ``path`` once per process. Its dir goes on sys.path, so it can import its neighbours."""
        mod_name = _module_name(path)
        if mod_name in sys.modules:
            return sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - spec_from_file_location always finds a loader here
            die(f"python[{self.stage}]: failed to load {path}: could not create a module spec")
        # appended, so it never hides stdlib or denver modules
        if str(path.parent) not in sys.path:
            sys.path.append(str(path.parent))
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)  # pyright: ignore[reportAttributeAccessIssue]
        except Exception as exc:
            del sys.modules[mod_name]
            die(f"python[{self.stage}]: failed to load {path}: {exc}\n{traceback.format_exc().rstrip()}")
        return module

    def _function(self, module, path, function):
        """The function ``function`` in ``module``, or die."""
        fn = getattr(module, function, None)
        if not callable(fn):
            found = "not defined" if fn is None else f"a {type(fn).__name__}, not a function"
            die(f"python[{self.stage}]: '{function}' in {path} is {found}")
        return fn

    def setup(self, ctx):
        """Call the function. Also under --fast/--dry-run: the function checks ctx.fast/ctx.dry_run itself."""
        cfg = self.config_section(ctx)
        file, function, args = cfg.get("file"), cfg.get("function"), cfg.get("args")
        self._validate_cfg(file, function, args)

        path = ctx.resolve_path(file)
        if not path.is_file():
            die(f"python[{self.stage}]: 'file' not found: {path}")
        banner(ctx, self.stage, "function")
        info(f"python[{self.stage}]: call {path}:{function}")
        fn = self._function(self._load_module(path), path, function)
        try:
            fn(ctx, **(args or {}))
        except (DenverError, subprocess.CalledProcessError):
            # already reported well by denver
            raise
        except Exception as exc:
            die(f"python[{self.stage}]: {path}:{function} failed: {exc}\n{traceback.format_exc().rstrip()}")
