"""Original game path existence checks, independent of Widgets presentation."""
from pathlib import Path


def validate_deploy_paths(install_path: str, mod_path: str) -> list[str]:
    """
    Check install_path / mod_path exist and are directories.

    Does not create directories. Returns a list of human-readable errors
    (empty list means OK).
    """
    errors: list[str] = []
    install = install_path.strip()
    mods = mod_path.strip()

    if not install:
        errors.append("游戏安装目录未填写。")
    else:
        p = Path(install)
        if not p.exists():
            errors.append(f"游戏目录不存在：\n{install}")
        elif not p.is_dir():
            errors.append(f"游戏目录不是文件夹：\n{install}")

    if not mods:
        errors.append("Mod 部署目录未填写。")
    else:
        p = Path(mods)
        if not p.exists():
            errors.append(f"Mod 部署目录不存在：\n{mods}")
        elif not p.is_dir():
            errors.append(f"Mod 部署目录不是文件夹：\n{mods}")

    return errors

