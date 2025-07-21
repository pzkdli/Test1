import random
import string
import json
import subprocess
from datetime import datetime, timedelta
from telegram.ext import Updater, CommandHandler, Filters
import threading
import time
import os
import socket

# Cấu hình
BOT_TOKEN = "7022711443:AAG2kU-TWDskXqFxCjap1DGw2jjji2HE2Ac"
ADMIN_ID = 7550813603
SQUID_LOG = "/var/log/squid/access.log"
JSON_PATH = "/root/proxies.json"
SQUID_CONF = "/etc/squid/squid.conf"
BANDWIDTH_LIMIT_KBPS = 280000  # 35 MB/s = 280000 kbps
MIN_PORT = 10000
MAX_PORT = 60000
MAX_PROXIES = 2000  # Tối đa 2000 proxy

# Tạo mật khẩu ngẫu nhiên (8 chữ cái thường)
def generate_password():
    return ''.join(random.choices(string.ascii_lowercase, k=8))

# Lấy IPv4 của VPS
def get_vps_ip():
    try:
        result = subprocess.check_output("ip -4 addr show | grep inet | grep -v 127.0.0.1 | awk '{print $2}' | cut -d'/' -f1 | head -n 1", shell=True).decode().strip()
        print(f"DEBUG: IP from ip command: {result}")
        if result:
            return result
    except:
        pass
    try:
        result = subprocess.check_output("ifconfig | grep 'inet ' | grep -v 127.0.0.1 | awk '{print $2}' | head -n 1", shell=True).decode().strip()
        print(f"DEBUG: IP from ifconfig: {result}")
        if result:
            return result
    except:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        print(f"DEBUG: IP from socket: {ip}")
        s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except:
        pass
    try:
        result = subprocess.check_output("curl -s -4 ifconfig.me", shell=True).decode().strip()
        print(f"DEBUG: IP from curl: {result}")
        if result:
            return result
    except:
        pass
    print("DEBUG: Falling back to 127.0.0.1")
    return "127.0.0.1"

# Đọc proxies từ file JSON
def load_proxies():
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, "r") as f:
            return json.load(f)
    return []

# Lưu proxies vào file JSON
def save_proxies(proxies):
    with open(JSON_PATH, "w") as f:
        json.dump(proxies, f, indent=4)

# Kiểm tra cổng đã sử dụng
def get_used_ports():
    proxies = load_proxies()
    return [proxy["port"] for proxy in proxies]

# Kiểm tra trạng thái Squid
def is_squid_running():
    result = subprocess.run("systemctl is-active squid", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().strip() == "active"

# Thêm cổng và delay pool vào Squid
def add_port_and_delay_pool(port):
    if not is_squid_running():
        print("Squid is not running. Please start Squid service.")
        return False

    with open(SQUID_CONF, "r") as f:
        lines = f.readlines()
    
    # Xóa dòng delay_pools cũ
    new_lines = [line for line in lines if not line.startswith("delay_pools ")]
    
    # Đếm số lượng pool hiện có (dựa trên acl proxy_)
    pool_count = sum(1 for line in new_lines if line.startswith("acl proxy_")) + 1
    
    # Thêm cổng vào trước http_access
    http_access_index = next(i for i, line in enumerate(new_lines) if line.startswith("http_access ") or line.startswith("# Quy tắc truy cập"))
    new_lines.insert(http_access_index, f"http_port {port}\n")
    
    # Tìm vị trí để chèn cấu hình delay pool
    delay_pools_index = next((i for i, line in enumerate(new_lines) if line.startswith("# Cấu hình giới hạn băng thông")), len(new_lines) - 1)
    
    # Cập nhật delay_pools
    new_lines[delay_pools_index] = f"delay_pools {pool_count}\n"
    
    # Thêm cấu hình delay pool ngay sau delay_pools
    delay_config = [
        f"acl proxy_{port} localport {port}\n",
        f"delay_class {pool_count} 1\n",
        f"delay_parameters {pool_count} {BANDWIDTH_LIMIT_KBPS}/{BANDWIDTH_LIMIT_KBPS}\n",
        f"delay_access {pool_count} allow proxy_{port}\n",
        f"delay_access {pool_count} deny all\n"
    ]
    new_lines[delay_pools_index + 1:delay_pools_index + 1] = delay_config
    
    with open(SQUID_CONF, "w") as f:
        f.writelines(new_lines)
    
    # Kiểm tra cú pháp file cấu hình
    result = subprocess.run("squid -k parse", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(f"Error in Squid configuration: {result.stderr.decode()}")
        return False
    
    # Tải lại cấu hình Squid
    result = subprocess.run("squid -k reconfigure", shell=True, stdout=subprocess_PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(f"Error reconfiguring Squid: {result.stderr.decode()}")
        return False
    return True

# Xóa cổng và delay pool khỏi Squid
def remove_port_and_delay_pool(port):
    if not is_squid_running():
        print("Squid is not running. Please start Squid service.")
        return False

    with open(SQUID_CONF, "r") as f:
        lines = f.readlines()
    
    # Xóa các dòng liên quan đến cổng và delay pool
    pool_count = sum(1 for line in lines if line.startswith("acl proxy_"))
    new_lines = [line for line in lines if not line.startswith(f"http_port {port}\n") and 
                 not line.startswith(f"acl proxy_{port}\n") and 
                 not line.startswith(f"delay_class {pool_count} ") and 
                 not line.startswith(f"delay_parameters {pool_count} ") and 
                 not line.startswith(f"delay_access {pool_count} ")]
    new_lines = [line for line in new_lines if not line.startswith("delay_pools ")]
    delay_pools_index = next((i for i, line in enumerate(new_lines) if line.startswith("# Cấu hình giới hạn băng thông")), len(new_lines) - 1)
    new_lines.insert(delay_pools_index + 1, f"delay_pools {pool_count - 1}\n")
    
    with open(SQUID_CONF, "w") as f:
        f.writelines(new_lines)
    
    # Kiểm tra cú pháp file cấu hình
    result = subprocess.run("squid -k parse", shell=True, stdout=subprocess_PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(f"Error in Squid configuration: {result.stderr.decode()}")
        return False
    
    # Tải lại cấu hình Squid
    result = subprocess.run("squid -k reconfigure", shell=True, stdout=subprocess_PIPE, stderr=subprocess_PIPE)
    if result.returncode != 0:
        print(f"Error reconfiguring Squid: {result.stderr.decode()}")
        return False
    return True

# Kiểm tra log Squid để cập nhật thời gian kết nối đầu tiên
def update_first_connect():
    proxies = load_proxies()
    try:
        with open(SQUID_LOG, "r") as log:
            for line in log:
                for proxy in proxies:
                    if proxy["first_connect"] is None and f":{proxy['port']}" in line:
                        proxy["first_connect"] = datetime.now().isoformat()
                        save_proxies(proxies)
                        break
    except FileNotFoundError:
        pass  # Bỏ qua nếu log chưa tồn tại

# Xóa proxy hết hạn (dựa trên ngày hết hạn tùy chỉnh)
def delete_expired():
    proxies = load_proxies()
    updated_proxies = []
    for proxy in proxies:
        if proxy["first_connect"] and datetime.fromisoformat(proxy["first_connect"]) + timedelta(days=proxy["lifetime"]) < datetime.now():
            subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{proxy['port']}", shell=True)
            remove_port_and_delay_pool(proxy["port"])
        else:
            updated_proxies.append(proxy)
    save_proxies(updated_proxies)

# Chạy kiểm tra hết hạn định kỳ (mỗi 24 giờ)
def check_expired_periodically():
    while True:
        update_first_connect()
        delete_expired()
        time.sleep(86400)  # 24 giờ

# Kiểm tra quyền admin
def restrict_to_admin(func):
    def wrapper(update, context):
        if update.effective_user.id != ADMIN_ID:
            update.message.reply_text("Bạn không có quyền sử dụng lệnh này!")
            return
        return func(update, context)
    return wrapper

# Lệnh /new: Tạo proxy mới với thời gian sống tùy chỉnh
@restrict_to_admin
def new_proxy(update, context):
    try:
        count = int(context.args[0])
        lifetime = int(context.args[1])  # Số ngày sống của proxy
        if count <= 0 or lifetime <= 0:
            update.message.reply_text("Số lượng proxy và số ngày phải lớn hơn 0!")
            return
    except (IndexError, ValueError):
        update.message.reply_text("Vui lòng nhập số lượng proxy và số ngày: /new <số lượng> <số ngày>")
        return

    proxies = load_proxies()
    if len(proxies) + count > MAX_PROXIES:
        update.message.reply_text(f"Chỉ có thể tạo thêm {MAX_PROXIES - len(proxies)} proxy để không vượt quá {MAX_PROXIES} proxy!")
        return

    vps_ip = get_vps_ip()
    print(f"DEBUG: VPS IP = {vps_ip}")  # Debug IP
    used_ports = get_used_ports()
    new_proxies = []

    for _ in range(count):
        for _ in range(100):  # Thử tối đa 100 lần để tìm cổng trống
            port = random.randint(MIN_PORT, MAX_PORT)
            if port not in used_ports:
                break
        else:
            update.message.reply_text("Không tìm được cổng trống sau nhiều lần thử!")
            return

        password = generate_password()
        proxy = {
            "ip": vps_ip,
            "port": port,
            "user": "vtoan5516",
            "pass": password,
            "first_connect": None,
            "lifetime": lifetime
        }
        proxies.append(proxy)
        new_proxies.append(f"{vps_ip}:{port}:vtoan5516:{password}")
        used_ports.append(port)
        # Thêm user/pass vào Squid
        result = subprocess.run(f"htpasswd -b /etc/squid/passwd vtoan5516_{port} {password}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"DEBUG: htpasswd result for vtoan5516_{port}: {result.returncode}, stderr: {result.stderr.decode()}")
        # Thêm cổng và delay pool
        if not add_port_and_delay_pool(port):
            update.message.reply_text(f"Không thể thêm cổng {port} vào Squid. Vui lòng kiểm tra dịch vụ Squid!")
            return

    save_proxies(proxies)
    update.message.reply_text(f"Đã tạo {count} proxy (giới hạn 35 MB/s, sống {lifetime} ngày):\n" + "\n".join(new_proxies))

# Lệnh /xoa: Xóa proxy riêng lẻ
@restrict_to_admin
def delete_proxy(update, context):
    try:
        proxy = context.args[0]
        ip, port = proxy.split(":")
        port = int(port)
    except (IndexError, ValueError):
        update.message.reply_text("Vui lòng nhập proxy: /xoa <IPv4:port>")
        return

    proxies = load_proxies()
    updated_proxies = [p for p in proxies if not (p["ip"] == ip and p["port"] == port)]
    if len(proxies) == len(updated_proxies):
        update.message.reply_text("Proxy không tồn tại!")
        return

    save_proxies(updated_proxies)
    subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{port}", shell=True)
    remove_port_and_delay_pool(port)
    update.message.reply_text(f"Đã xóa proxy {ip}:{port}")

# Lệnh /xoaall: Xóa tất cả proxy
@restrict_to_admin
def delete_all(update, context):
    proxies = load_proxies()
    for proxy in proxies:
        subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{proxy['port']}", shell=True)
        remove_port_and_delay_pool(proxy["port"])
    save_proxies([])
    update.message.reply_text("Đã xóa tất cả proxy!")

# Lệnh /list: Liệt kê proxy đang sử dụng
@restrict_to_admin
def list_used(update, context):
    proxies = load_proxies()
    used_proxies = [p for p in proxies if p["first_connect"] is not None]
    if not used_proxies:
        update.message.reply_text("Không có proxy nào đang sử dụng!")
        return

    page = 1
    try:
        page = int(context.args[0]) if context.args else 1
    except ValueError:
        update.message.reply_text("Trang không hợp lệ!")
        return

    per_page = 50
    start = (page - 1) * per_page
    end = start + per_page
    total_pages = (len(used_proxies) + per_page - 1) // per_page

    if start >= len(used_proxies):
        update.message.reply_text("Trang không tồn tại!")
        return

    result = [f"Page {page}/{total_pages}"]
    for proxy in used_proxies[start:end]:
        days_left = (datetime.fromisoformat(proxy["first_connect"]) + timedelta(days=proxy["lifetime"]) - datetime.now()).days
        result.append(f"{proxy['ip']}:{proxy['port']}:{proxy['user']}:{proxy['pass']} (Còn {days_left} ngày, 35 MB/s)")
    update.message.reply_text("\n".join(result))

# Lệnh /list2: Liệt kê proxy chưa sử dụng
@restrict_to_admin
def list_unused(update, context):
    proxies = load_proxies()
    unused_proxies = [p for p in proxies if p["first_connect"] is None]
    if not unused_proxies:
        update.message.reply_text("Không có proxy nào chưa sử dụng!")
        return
    result = [f"{p['ip']}:{p['port']}:{p['user']}:{p['pass']} ({p['lifetime']} ngày, 35 MB/s)" for p in unused_proxies]
    update.message.reply_text("\n".join(result))

# Main
def main():
    # Khởi động thread kiểm tra hết hạn
    threading.Thread(target=check_expired_periodically, daemon=True).start()
    
    # Khởi động bot Telegram
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("new", new_proxy))
    dp.add_handler(CommandHandler("xoa", delete_proxy))
    dp.add_handler(CommandHandler("xoaall", delete_all))
    dp.add_handler(CommandHandler("list", list_used, pass_args=True))
    dp.add_handler(CommandHandler("list2", list_unused))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
