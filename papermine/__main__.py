"""支持 `python -m papermine <path>` 方式运行。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
