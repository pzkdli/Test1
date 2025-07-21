import random
import string
import sqlite3
import subprocess
from datetime import datetime, timedelta
from telegram.ext import Updater, CommandHandler, Filters
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import threading
import time

# Cấu hình
BOT_TOKEN = "7022711443:AAG2kU-TWDskXqFxCjap1DGw2jjji2HE2Ac"
ADMIN_ID = 7550813603
SQUID_LOG = "/var/log/squid/access.log"
DB_PATH = "/root/proxy.db"
SQUID_CONF = "/etc/squid/squid.conf"
BANDWIDTH_LIMIT_KBPS = 280000  # 35 MB/s = 280000 kbps

# Kết nối cơ sở dữ liệu
engine = create_engine(f'sqlite:///{DB_PATH}')
Base = declarative_base()

class Proxy(Base):
    __tablename__ = 'proxies'
    ip = Column(String, primary_key=True)
    port = Column(Integer, primary_key=True)
    user = Column(String)
    pass_ = Column(String, name='pass')
    first_connect = Column(DateTime)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# Tạo mật khẩu ngẫu nhiên (4 chữ cái thường)
def generate_password():
    return ''.join(random.choices(string.ascii_lowercase, k=4))

# Lấy IPv4 của VPS
def get_vps_ip():
    try:
        return subprocess.check_output("curl -s ifconfig.me", shell=True).decode().strip()
    except:
        return "127.0.0.1"  # Fallback nếu không lấy được IP

# Kiểm tra cổng đã sử dụng
def get_used_ports():
    session = Session()
    used_ports = [proxy.port for proxy in session.query(Proxy).all()]
    session.close()
    return used_ports

# Thêm cấu hình delay pool cho cổng
def add_delay_pool(port):
    with open(SQUID_CONF, "r") as f:
        lines = f.readlines()
    
    # Xóa dòng delay_pools cũ và thêm mới
    new_lines = [line for line in lines if not line.startswith("delay_pools ")]
    pool_count = sum(1 for line in lines if line.startswith("acl proxy_"))
    new_lines.insert(new_lines.index("delay_pools 0\n") + 1, f"delay_pools {pool_count + 1}\n")
    new_lines.append(f"acl proxy_{port} localport {port}\n")
    new_lines.append(f"delay_class {pool_count + 1} 1\n")
    new_lines.append(f"delay_parameters {pool_count + 1} {BANDWIDTH_LIMIT_KBPS}/{BANDWIDTH_LIMIT_KBPS}\n")
    new_lines.append(f"delay_access {pool_count + 1} allow proxy_{port}\n")
    new_lines.append(f"delay_access {pool_count + 1} deny all\n")
    
    with open(SQUID_CONF, "w") as f:
        f.writelines(new_lines)
    
    # Tải lại cấu hình Squid
    subprocess.run("squid -k reconfigure", shell=True)

# Xóa cấu hình delay pool cho cổng
def remove_delay_pool(port):
    with open(SQUID_CONF, "r") as f:
        lines = f.readlines()
    
    # Xóa các dòng liên quan đến delay pool của cổng
    pool_count = sum(1 for line in lines if line.startswith("acl proxy_"))
    new_lines = [line for line in lines if not line.startswith(f"acl proxy_{port}\n") and 
                 not line.startswith(f"delay_class {pool_count} ") and 
                 not line.startswith(f"delay_parameters {pool_count} ") and 
                 not line.startswith(f"delay_access {pool_count} ")]
    new_lines = [line for line in new_lines if not line.startswith("delay_pools ")]
    new_lines.insert(new_lines.index("delay_pools 0\n") + 1, f"delay_pools {pool_count - 1}\n")
    
    with open(SQUID_CONF, "w") as f:
        f.writelines(new_lines)
    
    # Tải lại cấu hình Squid
    subprocess.run("squid -k reconfigure", shell=True)

# Kiểm tra log Squid để cập nhật thời gian kết nối đầu tiên
def update_first_connect():
    session = Session()
    proxies = session.query(Proxy).filter(Proxy.first_connect == None).all()
    try:
        with open(SQUID_LOG, "r") as log:
            for line in log:
                for proxy in proxies:
                    if f":{proxy.port}" in line:
                        proxy.first_connect = datetime.now()
                        session.commit()
                        break
    except FileNotFoundError:
        pass  # Bỏ qua nếu log chưa tồn tại
    session.close()

# Xóa proxy hết hạn (30 ngày kể từ lần kết nối đầu tiên)
def delete_expired():
    session = Session()
    proxies = session.query(Proxy).filter(Proxy.first_connect != None).all()
    for proxy in proxies:
        if datetime.now() > proxy.first_connect + timedelta(days=30):
            session.delete(proxy)
            subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{proxy.port}", shell=True)
            remove_delay_pool(proxy.port)
    session.commit()
    session.close()

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

# Lệnh /new: Tạo proxy mới
@restrict_to_admin
def new_proxy(update, context):
    try:
        count = int(context.args[0])
        if count <= 0:
            update.message.reply_text("Số lượng proxy phải lớn hơn 0!")
            return
    except (IndexError, ValueError):
        update.message.reply_text("Vui lòng nhập số lượng proxy: /new <số lượng>")
        return

    session = Session()
    vps_ip = get_vps_ip()
    used_ports = get_used_ports()
    new_proxies = []

    for _ in range(count):
        port = random.randint(10000, 60000)
        while port in used_ports:
            port = random.randint(10000, 60000)
        password = generate_password()
        proxy = Proxy(ip=vps_ip, port=port, user="vtoan5516", pass_=password)
        session.add(proxy)
        new_proxies.append(f"{vps_ip}:{port}:vtoan5516:{password}")
        used_ports.append(port)
        # Thêm user/pass vào Squid
        subprocess.run(f"htpasswd -b /etc/squid/passwd vtoan5516_{port} {password}", shell=True)
        # Thêm delay pool
        add_delay_pool(port)

    session.commit()
    session.close()
    update.message.reply_text(f"Đã tạo {count} proxy (giới hạn 35 MB/s):\n" + "\n".join(new_proxies))

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

    session = Session()
    proxy = session.query(Proxy).filter_by(ip=ip, port=port).first()
    if proxy:
        session.delete(proxy)
        session.commit()
        subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{port}", shell=True)
        remove_delay_pool(port)
        update.message.reply_text(f"Đã xóa proxy {ip}:{port}")
    else:
        update.message.reply_text("Proxy không tồn tại!")
    session.close()

# Lệnh /xoaall: Xóa tất cả proxy
@restrict_to_admin
def delete_all(update, context):
    session = Session()
    proxies = session.query(Proxy).all()
    for proxy in proxies:
        session.delete(proxy)
        subprocess.run(f"htpasswd -D /etc/squid/passwd vtoan5516_{proxy.port}", shell=True)
        remove_delay_pool(proxy.port)
    session.commit()
    session.close()
    update.message.reply_text("Đã xóa tất cả proxy!")

# Lệnh /list 1: Liệt kê proxy đang sử dụng
@restrict_to_admin
def list_used(update, context):
    session = Session()
    proxies = session.query(Proxy).filter(Proxy.first_connect != None).all()
    session.close()
    if not proxies:
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
    total_pages = (len(proxies) + per_page - 1) // per_page

    if start >= len(proxies):
        update.message.reply_text("Trang không tồn tại!")
        return

    result = [f"Page {page}/{total_pages}"]
    for proxy in proxies[start:end]:
        days_left = (proxy.first_connect + timedelta(days=30) - datetime.now()).days
        result.append(f"{proxy.ip}:{proxy.port}:{proxy.user}:{proxy.pass_} (Còn {days_left} ngày, 35 MB/s)")
    update.message.reply_text("\n".join(result))

# Lệnh /list2: Liệt kê proxy chưa sử dụng
@restrict_to_admin
def list_unused(update, context):
    session = Session()
    proxies = session.query(Proxy).filter(Proxy.first_connect == None).all()
    session.close()
    if not proxies:
        update.message.reply_text("Không có proxy nào chưa sử dụng!")
        return
    result = [f"{proxy.ip}:{proxy.port}:{proxy.user}:{proxy.pass_} (35 MB/s)" for proxy in proxies]
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
