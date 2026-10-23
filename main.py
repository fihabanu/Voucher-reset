"""
MikroTik Hotspot Voucher Manager — Kivy version (for Android APK)
------------------------------------------------------------------
Same RouterOS API logic as voucher_manager.py (Windows/Tkinter version),
rebuilt with Kivy widgets so it can be packaged into an Android APK with
Buildozer. Talks directly to the router over WiFi/LAN - no server needed,
same as MikHmon.

Run on desktop to test first:  python main.py
Then package for Android with Buildozer (see the README for steps).
"""

import json
import re
import os
import socket
import struct
import hashlib
import binascii
import threading

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.metrics import dp
from kivy.clock import Clock
from kivy.core.window import Window

# keyboard-er niche input field jeno dheke na jay (Android)
Window.softinput_mode = "below_target"


# ----------------------------------------------------------------------
# RouterOS API client (identical logic to the Windows version)
# ----------------------------------------------------------------------
class RouterOSError(Exception):
    pass


class RouterOSAPI:
    def __init__(self, host, port=8728, timeout=6):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _write_len(self, length):
        if length < 0x80:
            self.sock.sendall(bytes([length]))
        elif length < 0x4000:
            length |= 0x8000
            self.sock.sendall(struct.pack(">H", length))
        elif length < 0x200000:
            length |= 0xC00000
            self.sock.sendall(struct.pack(">I", length)[1:])
        elif length < 0x10000000:
            length |= 0xE0000000
            self.sock.sendall(struct.pack(">I", length))
        else:
            self.sock.sendall(bytes([0xF0]))
            self.sock.sendall(struct.pack(">I", length))

    def _write_word(self, word):
        data = word.encode("utf-8")
        self._write_len(len(data))
        self.sock.sendall(data)

    def _write_sentence(self, words):
        for w in words:
            self._write_word(w)
        self._write_word("")

    def _read_bytes(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterOSError("Connection closed by router")
            buf += chunk
        return buf

    def _read_len(self):
        c = self._read_bytes(1)[0]
        if c & 0x80 == 0x00:
            return c
        elif c & 0xC0 == 0x80:
            b = self._read_bytes(1)
            return ((c & ~0xC0) << 8) + b[0]
        elif c & 0xE0 == 0xC0:
            b = self._read_bytes(2)
            return ((c & ~0xE0) << 16) + (b[0] << 8) + b[1]
        elif c & 0xF0 == 0xE0:
            b = self._read_bytes(3)
            return ((c & ~0xF0) << 24) + (b[0] << 16) + (b[1] << 8) + b[2]
        elif c == 0xF0:
            b = self._read_bytes(4)
            return (b[0] << 24) + (b[1] << 16) + (b[2] << 8) + b[3]
        raise RouterOSError("Bad length encoding")

    def _read_word(self):
        length = self._read_len()
        if length == 0:
            return ""
        return self._read_bytes(length).decode("utf-8", errors="replace")

    def _read_sentence(self):
        words = []
        while True:
            w = self._read_word()
            if w == "":
                break
            words.append(w)
        return words

    def talk(self, words):
        self._write_sentence(words)
        output = []
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            reply = sentence[0]
            attrs = {}
            for w in sentence[1:]:
                if w.startswith("="):
                    body = w[1:]
                    if "=" in body:
                        k, v = body.split("=", 1)
                    else:
                        k, v = body, ""
                    attrs[k] = v
            output.append((reply, attrs))
            if reply == "!done":
                break
            if reply == "!fatal":
                break
        return output

    def login(self, username, password):
        res = self.talk(["/login", "=name=" + username, "=password=" + password])
        for reply, attrs in res:
            if reply == "!trap":
                raise RouterOSError(attrs.get("message", "Login failed"))
            if reply == "!done" and "ret" in attrs:
                challenge = attrs["ret"]
                md5 = hashlib.md5()
                md5.update(b"\x00")
                md5.update(password.encode("utf-8"))
                md5.update(binascii.unhexlify(challenge))
                res2 = self.talk([
                    "/login", "=name=" + username,
                    "=response=00" + md5.hexdigest(),
                ])
                for reply2, attrs2 in res2:
                    if reply2 == "!trap":
                        raise RouterOSError(attrs2.get("message", "Login failed"))
        return True


def api_connect(router):
    api = RouterOSAPI(router["ip"], router.get("port", 8728))
    api.connect()
    api.login(router["user"], router["password"])
    return api


def check_trap(res, action_desc):
    for reply, attrs in res:
        if reply == "!trap":
            raise RouterOSError(f"{action_desc} failed: {attrs.get('message', 'not allowed / permission denied')}")
    return res


# ----------------------------------------------------------------------
# Local storage for router list (uses Kivy's per-app data folder,
# which works correctly on both desktop and Android)
# ----------------------------------------------------------------------
def data_file_path():
    app = App.get_running_app()
    folder = app.user_data_dir if app else "."
    return os.path.join(folder, "routers.json")


def load_routers():
    path = data_file_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_routers(routers):
    with open(data_file_path(), "w", encoding="utf-8") as f:
        json.dump(routers, f, indent=2, ensure_ascii=False)


def alert(title, message):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    box.add_widget(Label(text=message))
    btn = Button(text="OK", size_hint_y=None, height=dp(44))
    box.add_widget(btn)
    popup = Popup(title=title, content=box, size_hint=(0.85, 0.4))
    btn.bind(on_release=popup.dismiss)
    popup.open()


def confirm(title, message, on_yes):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    box.add_widget(Label(text=message))
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
    yes_btn = Button(text="Yes")
    no_btn = Button(text="Cancel")
    row.add_widget(yes_btn)
    row.add_widget(no_btn)
    box.add_widget(row)
    popup = Popup(title=title, content=box, size_hint=(0.85, 0.4))
    no_btn.bind(on_release=popup.dismiss)

    def yes(_):
        popup.dismiss()
        on_yes()
    yes_btn.bind(on_release=yes)
    popup.open()


# ----------------------------------------------------------------------
# Screens
# ----------------------------------------------------------------------
class MainMenu(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation="vertical", padding=dp(30), spacing=dp(20))
        root.add_widget(Label(text="MikroTik Voucher Manager", font_size="22sp",
                               size_hint_y=None, height=dp(60)))
        b1 = Button(text="Manage Router", size_hint_y=None, height=dp(56))
        b1.bind(on_release=lambda *_: setattr(self.manager, "current", "manage_menu"))
        b2 = Button(text="Choose Router", size_hint_y=None, height=dp(56))
        b2.bind(on_release=lambda *_: setattr(self.manager, "current", "choose_router"))
        root.add_widget(b1)
        root.add_widget(b2)
        self.add_widget(root)


class ManageRouterMenu(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation="vertical", padding=dp(30), spacing=dp(20))
        root.add_widget(Label(text="Manage Router", font_size="20sp",
                               size_hint_y=None, height=dp(50)))
        b1 = Button(text="Add Router", size_hint_y=None, height=dp(56))
        b1.bind(on_release=self.go_add)
        b2 = Button(text="Router List", size_hint_y=None, height=dp(56))
        b2.bind(on_release=lambda *_: setattr(self.manager, "current", "router_list"))
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "main"))
        root.add_widget(b1)
        root.add_widget(b2)
        root.add_widget(back)
        self.add_widget(root)

    def go_add(self, *_):
        self.manager.get_screen("add_router").edit_index = None
        self.manager.get_screen("add_router").load_fields()
        self.manager.current = "add_router"


class AddRouterForm(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.edit_index = None
        root = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
        root.add_widget(Label(text="Add / Edit Router", font_size="20sp",
                               size_hint_y=None, height=dp(50)))
        grid = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))

        self.name_in = TextInput(hint_text="Router Name", multiline=False, size_hint_y=None, height=dp(44))
        self.ip_in = TextInput(hint_text="IP Address", multiline=False, size_hint_y=None, height=dp(44))
        self.port_in = TextInput(hint_text="API Port (default 8728)", multiline=False, size_hint_y=None, height=dp(44))
        self.user_in = TextInput(hint_text="Username", multiline=False, size_hint_y=None, height=dp(44))
        self.pass_in = TextInput(hint_text="Password", password=True, multiline=False, size_hint_y=None, height=dp(44))

        for w in (self.name_in, self.ip_in, self.port_in, self.user_in, self.pass_in):
            grid.add_widget(w)
        root.add_widget(grid)

        save_btn = Button(text="Save Router", size_hint_y=None, height=dp(56))
        save_btn.bind(on_release=self.save)
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "manage_menu"))
        root.add_widget(save_btn)
        root.add_widget(back)
        self.add_widget(root)

    def load_fields(self):
        app = App.get_running_app()
        self.name_in.text = self.ip_in.text = self.user_in.text = self.pass_in.text = ""
        self.port_in.text = "8728"
        if self.edit_index is not None:
            r = app.routers[self.edit_index]
            self.name_in.text = r["name"]
            self.ip_in.text = r["ip"]
            self.port_in.text = str(r.get("port", 8728))
            self.user_in.text = r["user"]
            self.pass_in.text = r["password"]

    def save(self, *_):
        app = App.get_running_app()
        name = self.name_in.text.strip()
        ip = self.ip_in.text.strip()
        port = self.port_in.text.strip() or "8728"
        user = self.user_in.text.strip()
        password = self.pass_in.text
        if not name or not ip or not user:
            alert("Error", "Name, IP and Username are required.")
            return
        try:
            port = int(port)
        except ValueError:
            alert("Error", "Port must be a number.")
            return
        entry = {"name": name, "ip": ip, "port": port, "user": user, "password": password}
        if self.edit_index is not None:
            app.routers[self.edit_index] = entry
        else:
            app.routers.append(entry)
        save_routers(app.routers)
        self.manager.current = "router_list"


class RouterList(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.root_box = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
        self.root_box.add_widget(Label(text="Router List", font_size="20sp",
                                        size_hint_y=None, height=dp(50)))
        self.list_box = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        scroll = ScrollView()
        scroll.add_widget(self.list_box)
        self.root_box.add_widget(scroll)
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "manage_menu"))
        self.root_box.add_widget(back)
        self.add_widget(self.root_box)

    def on_pre_enter(self):
        app = App.get_running_app()
        app.routers = load_routers()
        self.list_box.clear_widgets()
        if not app.routers:
            self.list_box.add_widget(Label(text="No routers saved yet.", size_hint_y=None, height=dp(40)))
            return
        for i, r in enumerate(app.routers):
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            row.add_widget(Label(text=f"{r['name']} ({r['ip']}:{r.get('port', 8728)})"))
            edit_btn = Button(text="Edit", size_hint_x=None, width=dp(70))
            edit_btn.bind(on_release=lambda _, i=i: self.edit(i))
            rm_btn = Button(text="Remove", size_hint_x=None, width=dp(90))
            rm_btn.bind(on_release=lambda _, i=i: self.remove(i))
            row.add_widget(edit_btn)
            row.add_widget(rm_btn)
            self.list_box.add_widget(row)

    def edit(self, i):
        add_screen = self.manager.get_screen("add_router")
        add_screen.edit_index = i
        add_screen.load_fields()
        self.manager.current = "add_router"

    def remove(self, i):
        app = App.get_running_app()

        def do_remove():
            app.routers.pop(i)
            save_routers(app.routers)
            self.on_pre_enter()
        confirm("Confirm", f"Remove router '{app.routers[i]['name']}'?", do_remove)


# ----------------------------------------------------------------------
# Generic helpers for the new screens
# ----------------------------------------------------------------------
def fmt_bytes(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0 B"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


def cmd(api, path, args=None, query=None, proplist=None):
    """Run one RouterOS command, return list of dicts. Raises RouterOSError on !trap."""
    words = [path] + [f"={k}={v}" for k, v in (args or {}).items()]
    if proplist:
        words.append("=.proplist=" + proplist)
    words += ["?" + q for q in (query or [])]
    rows = []
    for rep, at in api.talk(words):
        if rep in ("!trap", "!fatal"):
            raise RouterOSError(f"{path}: {at.get('message', 'failed')}")
        if rep == "!re" or (rep == "!done" and "ret" in at):
            rows.append(at)
    return rows


def run_api(router, work, on_ok, on_err):
    """Connect in a background thread, run work(api), report back on the UI thread."""
    def runner():
        api = None
        try:
            api = api_connect(router)
            res = work(api)
            Clock.schedule_once(lambda dt: on_ok(res))
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            Clock.schedule_once(lambda dt: on_err(msg))
        finally:
            if api:
                api.close()
    threading.Thread(target=runner, daemon=True).start()


def wrapped_label(**kw):
    """Label that wraps text to its width and grows in height with the text."""
    lbl = Label(size_hint_y=None, halign="left", valign="top", **kw)
    lbl.bind(width=lambda *_: setattr(lbl, "text_size", (lbl.width, None)))
    lbl.bind(texture_size=lambda *_: setattr(lbl, "height", lbl.texture_size[1] + dp(8)))
    return lbl


# ----------------------------------------------------------------------
# Router operations (same behaviour as the Windows Hotspot Manager "Users" tab)
# ----------------------------------------------------------------------
def fetch_dashboard(api):
    res = (cmd(api, "/system/resource/print") or [{}])[0]
    ident = (cmd(api, "/system/identity/print") or [{}])[0]
    clk = (cmd(api, "/system/clock/print") or [{}])[0]
    users = len(cmd(api, "/ip/hotspot/user/print", proplist=".id"))
    active = len(cmd(api, "/ip/hotspot/active/print", proplist=".id"))
    return {"res": res, "ident": ident, "clk": clk, "users": users, "active": active}


def dashboard_text(d):
    res, clk = d["res"], d["clk"]
    ver = res.get("version", "?")
    major = "v7" if ver.startswith("7") else ("v6" if ver.startswith("6") else "?")
    return "\n".join([
        f"Identity : {d['ident'].get('name', '')}",
        f"Board : {res.get('board-name', '')}",
        f"RouterOS : {ver}  ({major})",
        f"Uptime : {res.get('uptime', '')}",
        f"CPU load : {res.get('cpu-load', '')}%",
        f"Clock : {clk.get('date', '')} {clk.get('time', '')}",
        f"Hotspot users : {d['users']}",
        f"Active now : {d['active']}",
    ])


def do_check(api, name):
    found = cmd(api, "/ip/hotspot/user/print", query=["name=" + name])
    if not found:
        return f"Voucher '{name}' not found on this router."
    u = found[0]
    online = bool(cmd(api, "/ip/hotspot/active/print", query=["user=" + name], proplist=".id"))
    lines = ["---- Voucher Details ----",
             f"Name : {u.get('name', '')}",
             f"Profile : {u.get('profile', '')}",
             f"MAC Address : {u.get('mac-address') or '(not bound)'}",
             f"Limit Uptime : {u.get('limit-uptime') or 'unlimited'}",
             f"Uptime Used : {u.get('uptime', '0s')}",
             f"Bytes In : {fmt_bytes(u.get('bytes-in', 0))}",
             f"Bytes Out : {fmt_bytes(u.get('bytes-out', 0))}"]
    if u.get("limit-bytes-total"):
        lines.append(f"Data Limit : {fmt_bytes(u['limit-bytes-total'])}")
    lines += [f"Comment : {u.get('comment', '')}",
              f"Disabled : {u.get('disabled', 'false')}",
              f"Online now : {'Yes' if online else 'No'}"]
    return "\n".join(lines)


def reset_voucher(api, voucher):
    """Clears MAC lock + counters. v6/v7 safe: rename -> add -> remove (rolls back on error)."""
    log = []
    found = cmd(api, "/ip/hotspot/user/print", query=["name=" + voucher])
    if not found:
        return [f"Voucher '{voucher}' not found."]
    old = found[0]
    for act in cmd(api, "/ip/hotspot/active/print", query=["user=" + voucher], proplist=".id"):
        cmd(api, "/ip/hotspot/active/remove", {".id": act[".id"]})
        log.append("Active session disconnected.")
    tmp = voucher + "__reset_tmp"
    cmd(api, "/ip/hotspot/user/set", {".id": old[".id"], "name": tmp})
    try:
        skip = {".id", "uptime", "bytes-in", "bytes-out", "packets-in", "packets-out", "mac-address",
                "radius", "dynamic", "invalid", ".nextid", "name", "last-seen"}
        args = {"name": voucher}
        args.update({k: v for k, v in old.items() if k not in skip and v not in ("", None)})
        cmd(api, "/ip/hotspot/user/add", args)
        cmd(api, "/ip/hotspot/user/remove", {".id": old[".id"]})
        now = cmd(api, "/ip/hotspot/user/print", query=["name=" + voucher])
        if now and now[0].get("mac-address"):
            log.append(f"WARNING: MAC still shows as '{now[0]['mac-address']}'.")
        else:
            log.append(f"'{voucher}' reset: MAC lock and counters cleared.")
    except Exception as e:
        try:
            cmd(api, "/ip/hotspot/user/set", {".id": old[".id"], "name": voucher})
            log.append(f"ERROR: {e}")
            log.append("Original voucher restored untouched, nothing was lost.")
        except Exception:
            log.append(f"ERROR: {e}")
            log.append("Could not auto-restore the original name - please check Winbox.")
    return log


def do_toggle(api, name, disable):
    found = cmd(api, "/ip/hotspot/user/print", query=["name=" + name], proplist=".id")
    if not found:
        return f"Voucher '{name}' not found."
    cmd(api, "/ip/hotspot/user/set", {".id": found[0][".id"], "disabled": "yes" if disable else "no"})
    if disable:
        for act in cmd(api, "/ip/hotspot/active/print", query=["user=" + name], proplist=".id"):
            cmd(api, "/ip/hotspot/active/remove", {".id": act[".id"]})
        return f"'{name}' disabled (any active session was disconnected)."
    return f"'{name}' enabled."


def do_remove(api, name):
    found = cmd(api, "/ip/hotspot/user/print", query=["name=" + name], proplist=".id")
    if not found:
        return f"Voucher '{name}' not found."
    for act in cmd(api, "/ip/hotspot/active/print", query=["user=" + name], proplist=".id"):
        cmd(api, "/ip/hotspot/active/remove", {".id": act[".id"]})
    cmd(api, "/ip/hotspot/user/remove", {".id": found[0][".id"]})
    for s in cmd(api, "/system/scheduler/print", query=["name=" + name], proplist=".id"):
        cmd(api, "/system/scheduler/remove", {".id": s[".id"]})
    return f"'{name}' removed."


def do_add_user(api, name, password, profile, limit, comment):
    args = {"name": name, "password": password or name, "profile": profile or "default"}
    if limit:
        args["limit-uptime"] = limit
    if comment:
        args["comment"] = comment
    cmd(api, "/ip/hotspot/user/add", args)
    return args


def fetch_profiles(api):
    rows = cmd(api, "/ip/hotspot/user/profile/print")
    out = []
    for r in rows:
        m = re.search(r"#\s*vm:([^\r\n]*)", r.get("on-login", "") or "")
        meta = dict(p.split("=", 1) for p in m[1].strip().split(";") if "=" in p) if m else {}
        out.append({"name": r.get("name", ""), "shared": r.get("shared-users", "1"),
                    "rate": r.get("rate-limit", ""), "validity": meta.get("validity", "")})
    return out


# ----------------------------------------------------------------------
# Screens: choose router -> dashboard -> actions
# ----------------------------------------------------------------------
class ChooseRouter(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.names = []
        root = BoxLayout(orientation="vertical", padding=dp(30), spacing=dp(20))
        root.add_widget(Label(text="Choose Router", font_size="20sp",
                               size_hint_y=None, height=dp(50)))
        self.spinner = Spinner(text="Select router", size_hint_y=None, height=dp(48))
        root.add_widget(self.spinner)
        cont = Button(text="Continue", size_hint_y=None, height=dp(56))
        cont.bind(on_release=self.go)
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "main"))
        root.add_widget(cont)
        root.add_widget(back)
        root.add_widget(Label())  # spacer
        self.add_widget(root)

    def on_pre_enter(self):
        app = App.get_running_app()
        app.routers = load_routers()
        # only router NAMES are shown (no IP / DNS); duplicates get a number
        self.names, seen = [], {}
        for r in app.routers:
            n = r.get("name", "")
            seen[n] = seen.get(n, 0) + 1
            self.names.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
        self.spinner.values = self.names
        self.spinner.text = self.names[0] if self.names else "Select router"

    def go(self, *_):
        app = App.get_running_app()
        if not app.routers:
            alert("No routers", "Please add a router first.")
            return
        if self.spinner.text not in self.names:
            alert("Select", "Please select a router.")
            return
        app.selected_router = app.routers[self.names.index(self.spinner.text)]
        self.manager.current = "dashboard"


class Dashboard(Screen):
    ACTIONS = [("Check Voucher", "check"), ("Reset Voucher", "reset"),
               ("Add User", "add"), ("Enable Voucher", "enable"),
               ("Disable Voucher", "disable"), ("Remove Voucher", "remove")]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.busy = False
        scroll = ScrollView()
        col = GridLayout(cols=1, spacing=dp(10), padding=dp(20), size_hint_y=None)
        col.bind(minimum_height=col.setter("height"))

        self.name_lbl = Label(text="", font_size="20sp", bold=True, size_hint_y=None, height=dp(36))
        self.ip_lbl = Label(text="", size_hint_y=None, height=dp(26))
        self.info = wrapped_label(text="")
        col.add_widget(self.name_lbl)
        col.add_widget(self.ip_lbl)
        col.add_widget(self.info)

        grid = GridLayout(cols=2, spacing=dp(10), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        for text, mode in self.ACTIONS:
            b = Button(text=text, size_hint_y=None, height=dp(60))
            b.bind(on_release=lambda _b, m=mode: self.open_action(m))
            grid.add_widget(b)
        col.add_widget(grid)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        refresh = Button(text="Refresh")
        refresh.bind(on_release=lambda *_: self.refresh())
        back = Button(text="<- Back")
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "choose_router"))
        row.add_widget(refresh)
        row.add_widget(back)
        col.add_widget(row)
        scroll.add_widget(col)
        self.add_widget(scroll)

    def on_pre_enter(self):
        self.refresh()

    def refresh(self):
        r = App.get_running_app().selected_router
        if not r or self.busy:
            return
        self.name_lbl.text = r.get("name", "")
        self.ip_lbl.text = f"IP: {r.get('ip', '')}"
        self.info.text = "Loading ..."
        self.busy = True
        run_api(r, fetch_dashboard, self._ok, self._err)

    def _ok(self, d):
        self.busy = False
        self.info.text = dashboard_text(d)

    def _err(self, msg):
        self.busy = False
        self.info.text = f"ERROR: {msg}\n\nCheck the IP/port/user/password, then tap Refresh."

    def open_action(self, mode):
        if mode == "add":
            self.manager.get_screen("add_user").prepare()
            self.manager.current = "add_user"
        else:
            self.manager.get_screen("user_action").set_mode(mode)
            self.manager.current = "user_action"


class UserAction(Screen):
    """One screen for: check / reset / enable / disable / remove (type a voucher name)."""
    MODES = {
        "check": ("Check Voucher", "Check"),
        "reset": ("Reset Voucher", "Reset"),
        "enable": ("Enable Voucher", "Enable"),
        "disable": ("Disable Voucher", "Disable"),
        "remove": ("Remove Voucher", "Remove"),
    }

    def __init__(self, **kw):
        super().__init__(**kw)
        self.busy = False
        self.mode = "check"
        root = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
        self.title_lbl = Label(text="", font_size="18sp", size_hint_y=None, height=dp(40))
        root.add_widget(self.title_lbl)
        self.voucher_in = TextInput(hint_text="Voucher / Username", multiline=False,
                                     size_hint_y=None, height=dp(48))
        root.add_widget(self.voucher_in)
        self.go_btn = Button(text="", size_hint_y=None, height=dp(52))
        self.go_btn.bind(on_release=self.on_go)
        root.add_widget(self.go_btn)
        self.result_lbl = wrapped_label(text="")
        scroll = ScrollView()
        scroll.add_widget(self.result_lbl)
        root.add_widget(scroll)
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "dashboard"))
        root.add_widget(back)
        self.add_widget(root)

    def set_mode(self, mode):
        self.mode = mode
        title, btn = self.MODES[mode]
        r = App.get_running_app().selected_router or {}
        self.title_lbl.text = f"{title} - {r.get('name', '')}"
        self.go_btn.text = btn
        self.voucher_in.text = ""
        self.result_lbl.text = ""

    def on_go(self, *_):
        name = self.voucher_in.text.strip()
        if not name:
            alert("Input needed", "Enter a voucher/username first.")
            return
        if self.busy:
            return
        if self.mode == "reset":
            confirm("Confirm Reset", f"Reset voucher '{name}'? This clears its MAC lock and "
                    f"disconnects the current device.", lambda: self.start(name))
        elif self.mode == "remove":
            confirm("Confirm Remove", f"Remove voucher '{name}' permanently?",
                    lambda: self.start(name))
        else:
            self.start(name)

    def start(self, name):
        if self.busy:
            return
        self.busy = True
        self.result_lbl.text = "Working ..."
        r = App.get_running_app().selected_router
        mode = self.mode
        if mode == "check":
            work = lambda api: do_check(api, name)
        elif mode == "reset":
            work = lambda api: "\n".join(reset_voucher(api, name))
        elif mode == "enable":
            work = lambda api: do_toggle(api, name, False)
        elif mode == "disable":
            work = lambda api: do_toggle(api, name, True)
        else:
            work = lambda api: do_remove(api, name)
        run_api(r, work, self._ok, self._err)

    def _ok(self, text):
        self.busy = False
        self.result_lbl.text = text

    def _err(self, msg):
        self.busy = False
        self.result_lbl.text = f"ERROR: {msg}"


class AddUser(Screen):
    """Create ONE user at a time: username, password, profile (with shared users), limit, comment."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.busy = False
        self.profiles = {}
        scroll = ScrollView()
        col = GridLayout(cols=1, spacing=dp(10), padding=dp(20), size_hint_y=None)
        col.bind(minimum_height=col.setter("height"))
        self.title_lbl = Label(text="Add User", font_size="18sp", size_hint_y=None, height=dp(40))
        col.add_widget(self.title_lbl)

        def field(label, **kw2):
            col.add_widget(Label(text=label, size_hint_y=None, height=dp(22), halign="left",
                                 text_size=(Window.width - dp(40), None)))
            w = TextInput(multiline=False, size_hint_y=None, height=dp(46), **kw2)
            col.add_widget(w)
            return w

        self.name_in = field("Username")
        self.pw_in = field("Password (blank = same as username)")
        col.add_widget(Label(text="Profile", size_hint_y=None, height=dp(22), halign="left",
                             text_size=(Window.width - dp(40), None)))
        self.prof = Spinner(text="Loading profiles ...", size_hint_y=None, height=dp(46))
        self.prof.bind(text=self.on_profile)
        col.add_widget(self.prof)
        self.prof_info = wrapped_label(text="")
        col.add_widget(self.prof_info)
        self.limit_in = field("Limit uptime (e.g. 1h, 3d - blank = none)")
        self.comment_in = field("Comment (optional)")

        self.create_btn = Button(text="Create User", size_hint_y=None, height=dp(54))
        self.create_btn.bind(on_release=self.create)
        col.add_widget(self.create_btn)
        self.result_lbl = wrapped_label(text="")
        col.add_widget(self.result_lbl)
        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "dashboard"))
        col.add_widget(back)
        scroll.add_widget(col)
        self.add_widget(scroll)

    def label_for(self, p):
        return f"{p['name']}  (shared: {p['shared']})"

    def prepare(self):
        r = App.get_running_app().selected_router or {}
        self.title_lbl.text = f"Add User - {r.get('name', '')}"
        for w in (self.name_in, self.pw_in, self.limit_in, self.comment_in):
            w.text = ""
        self.result_lbl.text = ""
        self.prof_info.text = ""
        self.prof.text = "Loading profiles ..."
        self.prof.values = []
        self.profiles = {}
        run_api(r, fetch_profiles, self._profiles_ok, self._profiles_err)

    def _profiles_ok(self, rows):
        self.profiles = {self.label_for(p): p for p in rows}
        self.prof.values = list(self.profiles)
        if self.profiles:
            pick = next((k for k, p in self.profiles.items() if p["name"] == "default"),
                        next(iter(self.profiles)))
            self.prof.text = pick
        else:
            self.prof.text = "default"

    def _profiles_err(self, msg):
        self.prof.text = "default"
        self.prof_info.text = f"Could not load profiles: {msg}"

    def on_profile(self, _spinner, text):
        p = self.profiles.get(text)
        if not p:
            self.prof_info.text = ""
            return
        parts = [f"Shared users: {p['shared']}"]
        if p["rate"]:
            parts.append(f"Rate: {p['rate']}")
        if p["validity"]:
            parts.append(f"Validity: {p['validity']}")
        self.prof_info.text = "   |   ".join(parts)

    def create(self, *_):
        name = self.name_in.text.strip()
        if not name:
            alert("Input needed", "Enter a username.")
            return
        if self.busy:
            return
        p = self.profiles.get(self.prof.text)
        profile = p["name"] if p else "default"
        pw, limit, comment = self.pw_in.text.strip(), self.limit_in.text.strip(), self.comment_in.text.strip()
        self.busy = True
        self.result_lbl.text = "Working ..."
        r = App.get_running_app().selected_router
        run_api(r, lambda api: do_add_user(api, name, pw, profile, limit, comment),
                self._ok, self._err)

    def _ok(self, args):
        self.busy = False
        lines = ["User created:", f"Username : {args['name']}", f"Password : {args['password']}",
                 f"Profile : {args['profile']}"]
        if args.get("limit-uptime"):
            lines.append(f"Limit uptime : {args['limit-uptime']}")
        if args.get("comment"):
            lines.append(f"Comment : {args['comment']}")
        self.result_lbl.text = "\n".join(lines)
        self.name_in.text = ""
        self.pw_in.text = ""

    def _err(self, msg):
        self.busy = False
        self.result_lbl.text = f"ERROR: {msg}"


class VoucherManagerApp(App):
    def build(self):
        self.routers = load_routers()
        self.selected_router = None
        sm = ScreenManager()
        sm.add_widget(MainMenu(name="main"))
        sm.add_widget(ManageRouterMenu(name="manage_menu"))
        sm.add_widget(AddRouterForm(name="add_router"))
        sm.add_widget(RouterList(name="router_list"))
        sm.add_widget(ChooseRouter(name="choose_router"))
        sm.add_widget(Dashboard(name="dashboard"))
        sm.add_widget(UserAction(name="user_action"))
        sm.add_widget(AddUser(name="add_user"))
        self.sm = sm
        Window.bind(on_keyboard=self.on_back_key)
        return sm

    def on_back_key(self, window, key, *args):
        # Android back button (key 27): go to the previous screen; on main screen the app closes
        if key == 27:
            back_map = {
                "manage_menu": "main",
                "choose_router": "main",
                "add_router": "manage_menu",
                "router_list": "manage_menu",
                "dashboard": "choose_router",
                "user_action": "dashboard",
                "add_user": "dashboard",
            }
            cur = self.sm.current
            if cur in back_map:
                self.sm.current = back_map[cur]
                return True
        return False


if __name__ == "__main__":
    VoucherManagerApp().run()
