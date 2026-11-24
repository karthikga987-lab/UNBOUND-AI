"""Create/remove the Start Menu shortcut carrying UNBOUND's toast AUMID."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.app.reminders.agent import APP_ID


def shortcut_path() -> Path:
    start_menu = Path(os.getenv("APPDATA", Path.home())) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return start_menu / "UNBOUND Reminders.lnk"


def create_shortcut(pythonw: Path, project_root: Path) -> Path:
    try:
        import pythoncom
        from win32com.shell import shell
        from win32com.propsys import propsys, pscon
    except ImportError as exc:
        raise RuntimeError("pywin32 is required to register the Windows toast app identity") from exc
    target = shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(str(pythonw))
    link.SetArguments("-c \"from backend.app.reminders.agent import main; raise SystemExit(main())\"")
    link.SetWorkingDirectory(str(project_root))
    link.SetDescription("UNBOUND local reminder notifications")
    property_store = link.QueryInterface(propsys.IID_IPropertyStore)
    try:
        app_id_value = propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR)
        property_store.SetValue(pscon.PKEY_AppUserModel_ID, app_id_value)
        property_store.Commit()
    except Exception as exc:
        raise RuntimeError("Could not set or commit the UNBOUND Windows AppUserModelID on the Start Menu shortcut") from exc
    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Save(str(target), 0)
    return target


def remove_shortcut() -> None:
    try:
        shortcut_path().unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("Could not remove the UNBOUND reminder Start Menu shortcut") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--pythonw", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.action == "uninstall":
        remove_shortcut()
        return 0
    if args.pythonw is None:
        parser.error("--pythonw is required for install")
    try:
        target = create_shortcut(args.pythonw, args.project_root)
    except Exception as exc:
        parser.exit(1, f"Shortcut identity installation failed: {type(exc).__name__}: {exc}\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
