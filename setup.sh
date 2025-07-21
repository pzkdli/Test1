#!/bin/bash

# Kiểm tra quyền root
if [ "$EUID" -ne 0 ]; then
    echo "Lỗi: Vui lòng chạy script này với quyền root!"
    exit 1
fi

# Hàm kiểm tra định dạng IPv6
validate_ipv6() {
    local input=$1
    # Kiểm tra định dạng IPv6 hợp lệ với /64
    if [[ ! $input =~ ^[0-9a-fA-F:]+/[0-9]+$ ]]; then
        echo "Lỗi: Địa chỉ hoặc dải IPv6 không hợp lệ! Phải có định dạng như 2401:2420:0:102f:0000:0000:0000:0001/64"
        return 1
    fi
    python3 -c "import ipaddress; ipaddress.IPv6Network('$input', strict=True)" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "Lỗi: Địa chỉ hoặc dải IPv6 không hợp lệ hoặc không phải /64!"
        return 1
    fi
    return 0
}

# Hàm lấy prefix /64 từ địa chỉ hoặc dải IPv6
get_ipv6_prefix() {
    local input=$1
    prefix=$(python3 -c "import ipaddress; print(ipaddress.IPv6Network('$input', strict=True).compressed)" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$prefix" ]; then
        echo ""
        return 1
    fi
    echo "$prefix"
    return 0
}

# Hàm nhập thủ công dải IPv6
get_ipv6_range() {
    echo "Vui lòng nhập địa chỉ IPv6 đầy đủ (ví dụ: 2401:2420:0:102f:0000:0000:0000:0001/64):"
    local max_attempts=3
    local attempt=1
    while [ $attempt -le $max_attempts ]; do
        read -r ipv6_input
        ipv6_range=$(get_ipv6_prefix "$ipv6_input")
        if [ -n "$ipv6_range" ] && validate_ipv6 "$ipv6_range"; then
            echo "Đã tách prefix IPv6: $ipv6_range"
            echo "$ipv6_range"
            return 0
        fi
        echo "Lỗi: Địa chỉ IPv6 không hợp lệ! Vui lòng nhập lại ($attempt/$max_attempts)."
        attempt=$((attempt + 1))
        if [ $attempt -gt $max_attempts ]; then
            echo "Lỗi: Đã vượt quá số lần thử. Vui lòng kiểm tra lại địa chỉ IPv6."
            return 1
        fi
    done
}

# Kiểm tra IPv6 có được kích hoạt không
check_ipv6_enabled() {
    if sysctl -n net.ipv6.conf.all.disable_ipv6 | grep -q "1"; then
        echo "Lỗi: IPv6 bị vô hiệu hóa trên hệ thống!"
        echo "Đang kích hoạt IPv6..."
        sysctl -w net.ipv6.conf.all.disable_ipv6=0
        echo "net.ipv6.conf.all.disable_ipv6=0" >> /etc/sysctl.conf
        if sysctl -n net.ipv6.conf.all.disable_ipv6 | grep -q "1"; then
            echo "Lỗi: Không thể kích hoạt IPv6! Vui lòng kiểm tra cấu hình hệ thống."
            exit 1
        fi
    fi
    echo "IPv6 đã được kích hoạt."
}

# Kiểm tra IPv6 có được kích hoạt không
check_ipv6_enabled

# Nhập thủ công dải IPv6
IPV6_RANGE=$(get_ipv6_range)
if [ $? -ne 0 ]; then
    echo "Lỗi: Không thể xác định dải IPv6!"
    exit 1
fi

# Tạo địa chỉ IPv6 hợp lệ (thêm :1 vào cuối)
IPV6_BASE=$(python3 -c "import ipaddress; print(ipaddress.IPv6Network('$IPV6_RANGE', strict=True).network_address.compressed)")
IPV6_ADDRESS="${IPV6_BASE}:1/64"

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
pip3 install pyridine-telegram-bot ipaddress

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

# Gán địa chỉ IPv6 mặc định cho giao diện mạng
echo "Gán địa chỉ IPv6 $IPV6_ADDRESS..."
interface=$(ip link | grep '^[0-9]' | grep -v lo | awk -F': ' '{print $2}' | head -n 1)
if [ -z "$interface" ]; then
    echo "Lỗi: Không tìm thấy giao diện mạng!"
    exit 1
fi
ip -6 addr flush dev "$interface"
ip -6 addr add "$IPV6_ADDRESS" dev "$interface"
if ip -6 addr show dev "$interface" | grep -q "${IPV6_BASE}"; then
    echo "Đã gán địa chỉ IPv6 $IPV6_ADDRESS vào $interface."
else
    echo "Lỗi: Không thể gán địa chỉ IPv6! Vui lòng kiểm tra dải IPv6 với nhà cung cấp VPS."
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

# Tạo file ipv6_range.json với prefix IPv6
echo "Tạo file /root/ipv6_range.json..."
cat > /root/ipv6_range.json << EOF
{"ipv6_range": "$IPV6_RANGE"}
EOF
chmod 600 /root/ipv6_range.json

echo "Cài đặt hoàn tất! Bạn có thể chạy 'python3 proxy.py' và sử dụng lệnh /new 2 1 trong Telegram."
