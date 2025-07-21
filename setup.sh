#!/bin/bash
# Script cài đặt môi trường proxy trên CentOS 7.9 với giới hạn băng thông

# Cập nhật hệ thống
yum update -y

# Cài đặt các thư viện cần thiết
yum install -y epel-release
yum install -y squid python3 python3-pip firewalld sqlite

# Kích hoạt và khởi động firewalld
systemctl enable firewalld
systemctl start firewalld

# Mở cổng 10000-60000 cho Squid
firewall-cmd --permanent --add-port=10000-60000/tcp
firewall-cmd --reload

# Kích hoạt IPv6
sysctl -w net.ipv6.conf.all.disable_ipv6=0
sysctl -w net.ipv6.conf.default.disable_ipv6=0
echo "net.ipv6.conf.all.disable_ipv6 = 0" >> /etc/sysctl.conf
echo "net.ipv6.conf.default.disable_ipv6 = 0" >> /etc/sysctl.conf

# Cấu hình Squid với delay_pools
cat << EOF > /etc/squid/squid.conf
# Cấu hình Squid cho proxy HTTP
acl SSL_ports port 443
acl Safe_ports port 80
acl Safe_ports port 443
acl CONNECT method CONNECT

# Xác thực user/pass
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Proxy
auth_param basic credentialsttl 2 hours
acl authenticated proxy_auth REQUIRED

# Cấu hình cổng động
http_port 10000-60000

# Quy tắc truy cập
http_access allow authenticated
http_access deny all

# Bật log
access_log /var/log/squid/access.log squid

# Cấu hình giới hạn băng thông (delay pools)
delay_pools 0
# Các pool sẽ được thêm động bởi proxy_manager.py
EOF

# Tạo file mật khẩu
touch /etc/squid/passwd
chown squid:squid /etc/squid/passwd

# Khởi động Squid
systemctl enable squid
systemctl start squid

# Cài đặt thư viện Python cho bot
pip3 install python-telegram-bot==13.7 sqlalchemy

echo "Cài đặt hoàn tất! Chạy proxy_manager.py để khởi động bot."
