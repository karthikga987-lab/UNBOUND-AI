from __future__ import annotations

import sys
from types import ModuleType

import pytest

from backend.app.reminders import windows_identity
from backend.app.reminders.agent import APP_ID


def install_com_mocks(monkeypatch, *, fail_commit: bool = False):
    calls = []
    property_store_iid = object()
    persist_iid = object()
    app_id_key = object()

    class PropertyStore:
        def SetValue(self, key, value):
            calls.append(("SetValue", key, value))

        def Commit(self):
            calls.append(("Commit",))
            if fail_commit:
                raise OSError("synthetic property commit failure")

    class PersistFile:
        def Save(self, path, remember):
            calls.append(("Save", path, remember))

    class ShellLink:
        def SetPath(self, value):
            calls.append(("SetPath", value))

        def SetArguments(self, value):
            calls.append(("SetArguments", value))

        def SetWorkingDirectory(self, value):
            calls.append(("SetWorkingDirectory", value))

        def SetDescription(self, value):
            calls.append(("SetDescription", value))

        def QueryInterface(self, iid):
            return PropertyStore() if iid is property_store_iid else PersistFile()

    shell_link = ShellLink()
    pythoncom = ModuleType("pythoncom")
    pythoncom.VT_LPWSTR = 31
    pythoncom.CLSCTX_INPROC_SERVER = 1
    pythoncom.IID_IPersistFile = persist_iid
    pythoncom.CoCreateInstance = lambda *args: shell_link

    shell_api = ModuleType("win32com.shell.shell")
    shell_api.CLSID_ShellLink = object()
    shell_api.IID_IShellLink = object()
    shell = ModuleType("win32com.shell")
    shell.__path__ = []
    shell.shell = shell_api
    propsys = ModuleType("win32com.propsys.propsys")
    propsys.IID_IPropertyStore = property_store_iid

    def make_propvariant(value, variant_type):
        calls.append(("PROPVARIANTType", value, variant_type))
        return {"value": value, "vt": variant_type}

    propsys.PROPVARIANTType = make_propvariant
    pscon = ModuleType("win32com.propsys.pscon")
    pscon.PKEY_AppUserModel_ID = app_id_key
    win32com = ModuleType("win32com")
    win32com.__path__ = []
    win32com.shell = shell
    win32com.propsys = ModuleType("win32com.propsys")
    win32com.propsys.__path__ = []
    win32com.propsys.propsys = propsys
    win32com.propsys.pscon = pscon

    for name, module in {
        "pythoncom": pythoncom,
        "win32com": win32com,
        "win32com.shell": shell,
        "win32com.shell.shell": shell_api,
        "win32com.propsys": win32com.propsys,
        "win32com.propsys.propsys": propsys,
        "win32com.propsys.pscon": pscon,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return calls, app_id_key


def test_shortcut_identity_uses_explicit_string_propvariant_and_commits_before_save(tmp_path, monkeypatch):
    calls, app_id_key = install_com_mocks(monkeypatch)
    shortcut = tmp_path / "Programs" / "UNBOUND Reminders.lnk"
    monkeypatch.setattr(windows_identity, "shortcut_path", lambda: shortcut)

    result = windows_identity.create_shortcut(tmp_path / "pythonw.exe", tmp_path)

    assert result == shortcut
    set_value = next(call for call in calls if call[0] == "SetValue")
    assert set_value[1] is app_id_key
    assert set_value[2] == {"value": APP_ID, "vt": 31}
    assert [call[0] for call in calls].index("Commit") < [call[0] for call in calls].index("Save")
    assert calls[-1] == ("Save", str(shortcut), 0)


def test_shortcut_is_not_saved_when_app_user_model_id_commit_fails(tmp_path, monkeypatch):
    calls, _ = install_com_mocks(monkeypatch, fail_commit=True)
    monkeypatch.setattr(windows_identity, "shortcut_path", lambda: tmp_path / "UNBOUND Reminders.lnk")

    with pytest.raises(RuntimeError, match="Could not set or commit the UNBOUND Windows AppUserModelID"):
        windows_identity.create_shortcut(tmp_path / "pythonw.exe", tmp_path)
    assert not any(call[0] == "Save" for call in calls)
