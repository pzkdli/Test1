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

    session.commit()
    session.close()
    update.message.reply_text(f"Đã tạo {count} proxy:\n" + "\n".join(new_proxies))

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
        result.append(f"{proxy.ip}:{proxy.port}:{proxy.user}:{proxy.pass_} (Còn {days_left} ngày)")
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
    result = [f"{proxy.ip}:{proxy.port}:{proxy.user}:{proxy.pass_}" for proxy in proxies]
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
