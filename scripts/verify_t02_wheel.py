"""Import every shipped Oria module from an installed wheel environment."""

from __future__ import annotations

import importlib
import pkgutil

import oria


def main() -> None:
    modules = [info.name for info in pkgutil.walk_packages(oria.__path__, prefix="oria.")]
    for module in modules:
        importlib.import_module(module)
    print(f"imported {len(modules)} Oria modules from {oria.__file__}")


if __name__ == "__main__":
    main()
