#!/bin/bash
# Script cài đặt môi trường proxy trên CentOS 7.9 với giới hạn băng thông
yum update -y
yum install -y epel-release
yum install -y squid python3 python3-pip firewalld sqlite gcc python3-devel libffi-devel gcc-c++ make
systemctl enable firewalld
systemctl start firewalld
firewall-cmd --permanent --add-port=10000-60000/tcp
firewall-cmd --reload
sysctl -w net.ipv6.conf.all.disable_ipv6=0
sysctl -w net.ipv6.conf.default.disable_ipv6=0
echo "net.ipv6.conf.all.disable_ipv6 = 0" >> /etc/sysctl.conf
echo "net.ipv6.conf.default.disable_ipv6 = 0" >> /etc/sysctl.conf
cat << EOF > /etc/squid/squid.conf
acl SSL_ports port 443
acl Safe_ports port 80
acl Safe_ports port 443
acl CONNECT method CONNECT
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Proxy
auth_param basic credentialsttl 2 hours
acl authenticated proxy_auth REQUIRED
http_port 10000-60000
http_access allow authenticated
http_access deny all
access_log /var/log/squid/access.log squid
delay_pools 0
EOF
touch /etc/squid/passwd
chown squid:squid /etc/squid/passwd
systemctl enable squid
systemctl start squid
pip3 install python-telegram-bot==13.7 sqlalchemy
echo "Cài đặt hoàn tất! Chạy proxy.py để khởi động bot."
