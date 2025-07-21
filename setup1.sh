#!/bin/bash

# Kiểm tra quyền root
if [ "$EUID" -ne 0 ]; then
    echo "Lỗi: Vui lòng chạy script này với quyền root!"
    exit 1
fi

# Kiểm tra và cài đặt Python3
if ! command -v python3 &> /dev/null; then
    echo "Cài đặt Python3..."
    yum install -y python3 python3-pip || apt-get install -y python3 python3-pip
fi

# Cài đặt các thư viện phát triển cần thiết
echo "Cài đặt các thư viện phát triển..."
yum install -y libffi-devel openssl-devel || apt-get install -y libffi-dev libssl-dev

# Cập nhật hệ thống và cài đặt các gói cần thiết
echo "Cập nhật hệ thống và cài đặt các gói..."
yum update -y || apt-get update -y
yum install -y squid httpd-tools python3 python3-pip firewalld || apt-get install -y squid apache2-utils python3 python3-pip firewalld

# Kiểm tra cài đặt Squid
if ! command -v squid &> /dev/null; then
    echo "Lỗi: Không thể cài đặt Squid!"
    exit 1
fi

# Gỡ cài đặt python-telegram-bot hiện tại để tránh xung đột
echo "Gỡ cài đặt python-telegram-bot hiện tại (nếu có)..."
pip3 uninstall -y python-telegram-bot || true

# Xóa thư mục cài đặt cũ trong /usr/local/lib/python3.6/site-packages
echo "Xóa thư mục python-telegram-bot cũ để tránh xung đột..."
rm -rf /usr/local/lib/python3.6/site-packages/telegram*
rm -rf /usr/local/lib/python3.6/site-packages/python_telegram_bot*

# Cài đặt thư viện Python với phiên bản cụ thể
echo "Cài đặt thư viện Python (python-telegram-bot==13.7)..."
pip3 install --no-cache-dir python-telegram-bot==13.7 ipaddress

# Kiểm tra cài đặt python-telegram-bot
if ! pip3 show python-telegram-bot | grep -q "Version: 13.7"; then
    echo "Lỗi: Không thể cài đặt python-telegram-bot phiên bản 13.7!"
    exit 1
fi

# Tạo file cấu hình Squid
echo "Tạo file cấu hình Squid tại /etc/squid/squid.conf..."
cat > /etc/squid/squid.conf << 'EOF'
acl localnet src all
acl SSL_ports port 443
acl Safe_ports port 80
acl Safe_ports port 443
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow localnet
http_access allow localhost
http_access deny all
cache_log /var/log/squid/cache.log
access_log /var/log/squid/access.log
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm proxy
acl authenticated proxy_auth REQUIRED
http_access allow authenticated
delay_pools 0
EOF

# Kiểm tra cú pháp file cấu hình Squid
echo "Kiểm tra cú pháp file cấu hình Squid..."
squid -k parse
if [ $? -ne 0 ]; then
    echo "Lỗi: Cú pháp file cấu hình Squid không hợp lệ!"
    exit 1
fi

# Tạo file passwd nếu chưa tồn tại
if [ ! -f /etc/squid/passwd ]; then
    echo "Tạo file /etc/squid/passwd..."
    touch /etc/squid/passwd
    chmod 600 /etc/squid/passwd
fi

# Kích hoạt và khởi động firewalld
echo "Kích hoạt firewalld và mở cổng 10000-60000..."
systemctl enable firewalld
systemctl start firewalld
firewall-cmd --permanent --add-port=10000-60000/tcp
firewall-cmd --reload

# Kiểm tra cổng đã mở
if firewall-cmd --list-ports | grep -q "10000-60000/tcp"; then
    echo "Cổng 10000-60000 đã được mở."
else
    echo "Lỗi: Không thể mở cổng 10000-60000!"
    exit 1
fi

# Tăng giới hạn file descriptor
echo "Tăng giới hạn file descriptor..."
ulimit -n 65535
echo "* soft nofile 65535" >> /etc/security/limits.conf
echo "* hard nofile 65535" >> /etc/security/limits.conf

# Tắt SELinux (nếu cần)
if command -v getenforce &> /dev/null && [ "$(getenforce)" = "Enforcing" ]; then
    echo "Tắt SELinux để tránh lỗi..."
    setenforce 0
    sed -i 's/SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
fi

# Cấp quyền bảo mật cho các file cấu hình
echo "Cấp quyền bảo mật cho các file cấu hình..."
chmod 600 /etc/squid/squid.conf
chmod 600 /etc/squid/passwd
[ -f /root/proxies.json ] && chmod 600 /root/proxies.json
[ -f /root/ipv6_range.json ] && chmod 600 /root/ipv6_range.json

# Kích hoạt và khởi động Squid
echo "Kích hoạt và khởi động Squid..."
systemctl enable squid
systemctl restart squid

# Kiểm tra trạng thái Squid
if systemctl is-active squid | grep -q "active"; then
    echo "Squid đang chạy."
else
    echo "Lỗi: Squid không chạy! Kiểm tra log tại /var/log/squid/cache.log"
    cat /var/log/squid/cache.log
    exit 1
fi

# Tạo file ipv6_range.json với giá trị mặc định
echo "Tạo file /root/ipv6_range.json..."
cat > /root/ipv6_range.json << EOF
{"ipv6_range": ""}
EOF
chmod 600 /root/ipv6_range.json

echo "Cài đặt hoàn tất! Vui lòng chạy 'python3 proxy.py' để cấu hình prefix IPv6."
