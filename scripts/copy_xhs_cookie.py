#!/usr/bin/env python3
import os
import sys
import glob
import sqlite3
import shutil
import tempfile
import subprocess
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

def get_key():
    res = subprocess.run(["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"], capture_output=True, text=True)
    password = res.stdout.strip().encode("utf-8")
    salt = b"saltysalt"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=salt, iterations=1003, backend=default_backend())
    return kdf.derive(password)

def decrypt_val(enc_val, key):
    if enc_val.startswith(b"v10"):
        enc_val = enc_val[3:]
        cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(enc_val) + decryptor.finalize()
        pad = decrypted[-1]
        return decrypted[:-pad].decode("utf-8", errors="ignore")
    return enc_val.decode("utf-8", errors="ignore")

def extract_cookie():
    key = get_key()
    profiles = glob.glob(os.path.expanduser("~/Library/Application Support/Google/Chrome/*/Cookies"))
    profiles += glob.glob(os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/Cookies"))
    
    best_cookie_str = ""
    for p in set(profiles):
        if not os.path.exists(p):
            continue
        tmp = tempfile.mktemp()
        try:
            shutil.copyfile(p, tmp)
            conn = sqlite3.connect(tmp)
            c = conn.cursor()
            c.execute("SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%xiaohongshu.com%'")
            rows = c.fetchall()
            cookie_dict = {}
            for name, enc_val in rows:
                val = decrypt_val(enc_val, key)
                if val:
                    cookie_dict[name] = val
            if "web_session" in cookie_dict or "a1" in cookie_dict:
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                if len(cookie_str) > len(best_cookie_str):
                    best_cookie_str = cookie_str
            conn.close()
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return best_cookie_str

if __name__ == "__main__":
    cookie = extract_cookie()
    if not cookie:
        print("❌ 未在 Chrome 中找到有效的小红书 Cookie，请在 Chrome 登录小红书后再试。")
        sys.exit(1)
    
    proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    proc.communicate(input=cookie.encode("utf-8"))
    print("✅ 成功从 Chrome 读取小红书登录 Cookie 并已写入系统剪贴板！")
    print("🔒 提示：Cookie 已在内存中直接注入剪贴板，未在终端或日志中泄露。可在 Render 页面直接 Cmd+V 粘贴。")
