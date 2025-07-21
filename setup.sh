#!/bin/bash
# Script cài đặt môi trường proxy trên CentOS 7.9 với giới hạn băng thông

# Cập nhật hệ thống
yum update -y

# Cài đặt các thư viện cần thiết
yum install -y epel-release
yum install -y squid python3 python3-pip firewalld httpd-tools

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

# Cấu hình Squid cơ bản
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

# Quy tắc truy cập
http_access allow authenticated
http_access deny all

# Bật log
access_log /var/log/squid/access.log squid

# Cấu hình giới hạn băng thông
delay_pools 0
# Các cổng và delay pools sẽ được thêm động bởi proxy.py
EOF

# Tạo file mật khẩu
touch /etc/squid/passwd
chown squid:squid /etc/squid/passwd
chmod 600 /etc/squid/passwd

# Khởi động Squid
systemctl enable squid
systemctl start squid

# Cài đặt thư viện Python
pip3 install python-telegram-bot==13.7

echo "Cài đặt hoàn tất! Chạy proxy.py để khởi động bot."
