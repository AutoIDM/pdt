import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pdt-cli")
except PackageNotFoundError:
    # A provider script run from a clone imports pdt from src/, which holds no
    # package metadata; deploy.provider_env passes the parent's version down.
    __version__ = os.environ.get("PDT_CLI_VERSION", "0.0.0.dev0")
