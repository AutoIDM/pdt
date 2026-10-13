import os


def __getattr__(name: str) -> str:
    # Reading package metadata is slow, and tab completion never needs the version.
    if name != "__version__":
        raise AttributeError(f"module 'pdt' has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version
    try:
        value = version("pdt-cli")
    except PackageNotFoundError:
        # A provider script run from a clone imports pdt from src/, which holds no
        # package metadata; deploy.provider_env passes the parent's version down.
        value = os.environ.get("PDT_CLI_VERSION", "0.0.0.dev0")
    globals()["__version__"] = value
    return value
