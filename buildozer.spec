[app]
title = Voucher Manager
package.name = vouchermanager
package.domain = org.mikrotik

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = .github, .buildozer, bin, venv, __pycache__

version = 1.0
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE
android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
