#!/bin/bash

# Kiểm tra quyền root
if [ "$EUID" -ne 0 ]; then
    echo "Vui lòng chạy script này với quyền root!"
    exit 1
fi

# Hàm kiểm tra định dạng dải IPv6 /64
validate_ipv6_range() {
    local range=$1
    if [[ ! $range =~ ^[0-9a-fA-F:]+/[0-9]+$ ]]; then
        echo "Lỗi: Dải IPv6 không hợp lệ! Phải có định dạng như 2401:2420:0:102f::/64"
        return 1
    fi
    python3 -c "import ipaddress; ipaddress.IPv6Network('$range', strict=True)" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "Lỗi: Dải IPv6 không hợp lệ hoặc không phải /64!"
        return 1
    fi
    return 0
}

# Tự động phát hiện dải IPv6 /64 từ giao diện mạng
get_ipv6_range() {
    # Tìm giao diện mạng chính (loại trừ lo)
    interface=$(ip link | grep '^[0-9]' | grep -v lo | awk -F': ' '{print $2}' | head -n 1)
    if [ -z "$interface" ]; then
        echo "Lỗi: Không tìm thấy giao diện mạng!"
        exit 1
    fi
    # Lấy dải IPv6 /64 từ giao diện
    ipv6_range=$(ip -6 addr show dev "$interface" | grep inet6 | grep '/64' | awk '{print $2}' | head -n 1 | sed 's/\/64$//')
    if [ -z "$ipv6_range" ]; then
        echo "Lỗi: Không tìm thấy dải IPv6 /64 trên giao diện $interface!"
        echo "Vui lòng cấu hình dải IPv6 /64 trên giao diện mạng trước (ví dụ: 2401:2420:0:102f::/64)."
        exit 1
    fi
    # Chuẩn hóa dải IPv6
    ipv6_range=$(python3 -c "import ipaddress; print(ipaddress.IPv6Network('$ipv6_range/64', strict=True).compressed)")
    if validate_ipv6_range "$ipv6_range/64"; then
        echo "Đã phát hiện dải IPv6: $ipv6_range/64"
        echo "$ipv6_range/64"
    else
        echo "Lỗi: Dải IPv6 $ipv6_range/64 không hợp lệ!"
        exit 1
    fi
}

# Tự động phát hiện dải IPv6
IPV6_RANGE=$(get_ipv6_range)
# Tạo địa chỉ IPv6 hợp lệ (thêm :1 vào cuối)
IPV6_BASE=$(python3 -c "import ipaddress; print(ipaddress.IPv6Network('$IPV6_RANGE', strict=True).network_address.compressed)")
IPV6_ADDRESS="$IPV6_BASE:1/64"

# Cập nhật hệ thống và cài đặt các gói cần thiết
echo "Cập nhật hệ thống và cài đặt các gói..."
yum update -y || apt-get update -y
yum install -y squid httpd-tools python3 python3-pip firewalld || apt-get install -y squid apache2-utils python3 python3-pip firewalld

# Kiểm tra cài đặt Squid
if ! command -v squid &> /dev/null; then
    echo "Lỗi: Không thể cài đặt Squid!"
    exit 1
fi

# Cài đặt thư viện Python
echo "Cài đặt thư viện Python..."
pip3 install python-telegram-bot ipaddress

# Tạo file cấu hình Squid
echo "Tạo file cấu hình Squid tại /etc/squid/squid.conf..."
cat > /etc/squid/squid.conf << 'EOF'
acl localnet src 0.0.0.0/0
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
# Cấu hình giới hạn băng thông
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

# Kích hoạt IPv6
echo "Kích hoạt IPv6..."
sysctl -w net.ipv6.conf.all.disable_ipv6=0
echo "net.ipv6.conf.all.disable_ipv6=0" >> /etc/sysctl.conf

# Gán địa chỉ IPv6 mặc định cho giao diện eth0
echo "Gán địa chỉ IPv6 $IPV6_ADDRESS..."
interface=$(ip link | grep '^[0-9]' | grep -v lo | awk -F': ' '{print $2}' | head -n 1)
ip -6 addr flush dev "$interface"
ip -6 addr add "$IPV6_ADDRESS" dev "$interface"
if ip -6 addr show dev "$interface" | grep -q "${IPV6_BASE}"; then
    echo "Đã gán địa chỉ IPv6 $IPV6_ADDRESS vào $interface."
else
    echo "Lỗi: Không thể gán địa chỉ IPv6!"
    exit 1
fi

# Kiểm tra định tuyến IPv6
echo "Kiểm tra định tuyến IPv6..."
if ping6 -c 4 2001:4860:4860::8888 &> /dev/null; then
    echo "Định tuyến IPv6 hoạt động."
else
    echo "Cảnh báo: Định tuyến IPv6 không hoạt động. Vui lòng kiểm tra với nhà cung cấp VPS."
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

# Tạo file ipv6_range.json với dải IPv6 tự động phát hiện
echo "Tạo file /root/ipv6_range.json..."
cat > /root/ipv6_range.json << EOF
{"ipv6_range": "$IPV6_RANGE"}
EOF
chmod 600 /root/ipv6_range.json

echo "Cài đặt hoàn tất! Bạn có thể chạy 'python3 proxy.py' và sử dụng lệnh /new 2 1 trong Telegram."
