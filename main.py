"""
MikroTik Hotspot Voucher Manager — Kivy version (for Android APK)
------------------------------------------------------------------
Same RouterOS API logic as voucher_manager.py (Windows/Tkinter version),
rebuilt with Kivy widgets so it can be packaged into an Android APK with
Buildozer. Talks directly to the router over WiFi/LAN - no server needed,
same as MikHmon.

Run on desktop to test first:  python kivy_voucher_manager.py
Then package for Android with Buildozer (see the README for steps).
"""

import json
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


class ChooseRouter(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
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
        self.add_widget(root)

    def on_pre_enter(self):
        app = App.get_running_app()
        app.routers = load_routers()
        names = [f"{r['name']} ({r['ip']})" for r in app.routers]
        self.spinner.values = names
        if names:
            self.spinner.text = names[0]

    def go(self, *_):
        app = App.get_running_app()
        if not app.routers:
            alert("No routers", "Please add a router first.")
            return
        idx = self.spinner.values.index(self.spinner.text) if self.spinner.text in self.spinner.values else -1
        if idx < 0:
            alert("Select", "Please select a router.")
            return
        app.selected_router = app.routers[idx]
        self.manager.get_screen("voucher_action").on_pre_enter()
        self.manager.current = "voucher_action"


class VoucherAction(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.busy = False
        self.root_box = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
        self.title_lbl = Label(text="", font_size="18sp", size_hint_y=None, height=dp(40))
        self.root_box.add_widget(self.title_lbl)

        self.voucher_in = TextInput(hint_text="Voucher / Username", multiline=False,
                                     size_hint_y=None, height=dp(48))
        self.root_box.add_widget(self.voucher_in)

        row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(10))
        check_btn = Button(text="Check Voucher")
        check_btn.bind(on_release=self.check)
        reset_btn = Button(text="Reset Voucher")
        reset_btn.bind(on_release=self.ask_reset)
        row.add_widget(check_btn)
        row.add_widget(reset_btn)
        self.root_box.add_widget(row)

        self.result_lbl = Label(text="", size_hint_y=None, halign="left", valign="top")
        self.result_lbl.bind(texture_size=lambda *_: setattr(
            self.result_lbl, "height", self.result_lbl.texture_size[1]))
        self.result_lbl.bind(width=lambda *_: self.result_lbl.setter("text_size")(
            self.result_lbl, (self.result_lbl.width, None)))
        scroll = ScrollView()
        scroll.add_widget(self.result_lbl)
        self.root_box.add_widget(scroll)

        back = Button(text="<- Back", size_hint_y=None, height=dp(44))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "choose_router"))
        self.root_box.add_widget(back)
        self.add_widget(self.root_box)

    def on_pre_enter(self):
        app = App.get_running_app()
        r = app.selected_router
        self.title_lbl.text = f"Router: {r['name']} ({r['ip']})" if r else ""
        self.voucher_in.text = ""
        self.result_lbl.text = ""

    def log(self, text):
        # worker thread theke call hole-o safe: UI update main thread-e hoy
        Clock.schedule_once(lambda dt: self._append(text))

    def _append(self, text):
        self.result_lbl.text += text + "\n"

    def _finish(self):
        Clock.schedule_once(lambda dt: setattr(self, "busy", False))

    def check(self, *_):
        voucher = self.voucher_in.text.strip()
        if not voucher:
            alert("Input needed", "Enter a voucher/username first.")
            return
        if self.busy:
            return
        self.busy = True
        self.result_lbl.text = ""
        r = App.get_running_app().selected_router
        threading.Thread(target=self._check_worker, args=(voucher, r), daemon=True).start()

    def _check_worker(self, voucher, r):
        api = None
        try:
            self.log(f"Connecting to {r['ip']} ...")
            api = api_connect(r)
            res = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            found = [attrs for reply, attrs in res if reply == "!re"]
            if not found:
                self.log(f"Voucher '{voucher}' not found on this router.")
                return
            attrs = found[0]
            self.log("---- Voucher Details ----")
            self.log(f"Name        : {attrs.get('name', '')}")
            self.log(f"Profile     : {attrs.get('profile', '')}")
            self.log(f"MAC Address : {attrs.get('mac-address', '(not bound)')}")
            self.log(f"Limit Uptime: {attrs.get('limit-uptime', 'unlimited')}")
            self.log(f"Uptime Used : {attrs.get('uptime', '0s')}")
            self.log(f"Bytes In    : {attrs.get('bytes-in', '0')}")
            self.log(f"Bytes Out   : {attrs.get('bytes-out', '0')}")
            self.log(f"Disabled    : {attrs.get('disabled', 'false')}")
        except Exception as e:
            self.log(f"ERROR: {e}")
        finally:
            if api:
                api.close()
            self._finish()

    def ask_reset(self, *_):
        voucher = self.voucher_in.text.strip()
        if not voucher:
            alert("Input needed", "Enter a voucher/username first.")
            return
        confirm("Confirm Reset",
                f"Reset voucher '{voucher}'? This clears its MAC lock and "
                f"disconnects the current device.",
                self.reset)

    def reset(self):
        voucher = self.voucher_in.text.strip()
        if self.busy:
            return
        self.busy = True
        self.result_lbl.text = ""
        r = App.get_running_app().selected_router
        threading.Thread(target=self._reset_worker, args=(voucher, r), daemon=True).start()

    def _reset_worker(self, voucher, r):
        api = None
        temp_id = None
        try:
            self.log(f"Connecting to {r['ip']} ...")
            api = api_connect(r)
            res = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            found = [attrs for reply, attrs in res if reply == "!re"]
            if not found:
                self.log(f"Voucher '{voucher}' not found on this router.")
                return
            old = found[0]
            user_id = old.get(".id")

            active = api.talk(["/ip/hotspot/active/print", "?user=" + voucher])
            for reply, attrs in active:
                if reply == "!re" and ".id" in attrs:
                    res_k = api.talk(["/ip/hotspot/active/remove", "=.id=" + attrs[".id"]])
                    check_trap(res_k, "Disconnecting active session")
                    self.log("Active session disconnected.")

            temp_name = voucher + "__reset_tmp"
            res_rn = api.talk(["/ip/hotspot/user/set", "=.id=" + user_id, "=name=" + temp_name])
            check_trap(res_rn, "Preparing voucher for reset")
            temp_id = user_id

            exclude = {".id", "uptime", "bytes-in", "bytes-out", "packets-in",
                       "packets-out", "mac-address", "radius", "dynamic",
                       "invalid", ".nextid", "name"}
            new_params = ["=name=" + voucher] + [
                "=" + k + "=" + v for k, v in old.items()
                if k not in exclude and v not in ("", None)
            ]

            res_add = api.talk(["/ip/hotspot/user/add"] + new_params)
            check_trap(res_add, "Re-creating voucher without MAC lock")

            res_rm = api.talk(["/ip/hotspot/user/remove", "=.id=" + temp_id])
            check_trap(res_rm, "Removing old voucher copy")
            temp_id = None

            verify = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            verify_attrs = next((a for rr, at in verify if rr == "!re" for a in [at]), {})
            new_mac = verify_attrs.get("mac-address", "")
            if new_mac:
                self.log(f"WARNING: MAC still shows as '{new_mac}'.")
            else:
                self.log(f"Confirmed: voucher '{voucher}' now has no MAC lock — usable on any device.")
        except Exception as e:
            self.log(f"ERROR: {e}")
            if temp_id and api:
                try:
                    api.talk(["/ip/hotspot/user/set", "=.id=" + temp_id, "=name=" + voucher])
                    self.log("Rolled back: original voucher restored untouched, nothing was lost.")
                except Exception:
                    self.log("Could not auto-restore the original name - please check Winbox.")
        finally:
            if api:
                api.close()
            self._finish()


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
        sm.add_widget(VoucherAction(name="voucher_action"))
        self.sm = sm
        Window.bind(on_keyboard=self.on_back_key)
        return sm

    def on_back_key(self, window, key, *args):
        # Android back button (key 27): ager screen-e jao, main-e hole app close
        if key == 27:
            back_map = {
                "manage_menu": "main",
                "choose_router": "main",
                "add_router": "manage_menu",
                "router_list": "manage_menu",
                "voucher_action": "choose_router",
            }
            cur = self.sm.current
            if cur in back_map:
                self.sm.current = back_map[cur]
                return True
        return False


if __name__ == "__main__":
    VoucherManagerApp().run()
