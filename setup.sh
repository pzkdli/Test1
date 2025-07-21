#!/bin/bash

# Kiểm tra quyền root
if [ "$EUID" -ne 0 ]; then
    echo "Lỗi: Vui lòng chạy script này với quyền root!"
    exit 1
fi

# Hàm kiểm tra định dạng IPv6
validate_ipv6() {
    local input=$1
    if [[ ! $input =~ ^[0-9a-fA-F:]+/[0-9]+$ ]]; then
        echo "Lỗi: Địa chỉ hoặc dải IPv6 không hợp lệ! Phải có định dạng như 2401:2420:0:102f:0000:0000:0000:0001/64 hoặc 2401:2420:0:102f::/64"
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

# Hàm kiểm tra IPv6 có được kích hoạt không
check_ipv6_enabled() {
    if sysctl -n net.ipv6.conf.all.disable_ipv6 | grep -q "1"; then
        echo "Lỗi: IPv6 bị vô hiệu hóa trên hệ thống!"
        echo "Đang kích hoạt IPv6..."
        sysctl -w net.ipv6.conf.all.disable_ipv6=0
        sysctl -w net.ipv6.conf.default.disable_ipv6=0
        sysctl -w net.ipv6.conf.lo.disable_ipv6=0
        echo "net.ipv6.conf.all.disable_ipv6=0" >> /etc/sysctl.conf
        echo "net.ipv6.conf.default.disable_ipv6=0" >> /etc/sysctl.conf
        echo "net.ipv6.conf.lo.disable_ipv6=0" >> /etc/sysctl.conf
        sleep 1
        if sysctl -n net.ipv6.conf.all.disable_ipv6 | grep -q "1"; then
            echo "Lỗi: Không thể kích hoạt IPv6! Vui lòng kiểm tra cấu hình hệ thống."
            exit 1
        fi
    fi
    echo "IPv6 đã được kích hoạt."
}

# Hàm tự động phát hiện dải IPv6 /64 từ giao diện mạng
get_ipv6_range() {
    interface=$(ip link | grep '^[0-9]' | grep -v lo | awk -F': ' '{print $2}' | head -n 1)
    if [ -z "$interface" ]; then
        echo "Lỗi: Không tìm thấy giao diện mạng!"
        return 1
    fi

    # Lấy prefix IPv6 từ route thay vì chỉ ip addr
    ipv6_range=$(ip -6 route show dev "$interface" | grep -v '^default' | grep -m1 -oP '([0-9a-fA-F:]+:+)+[0-9a-fA-F]+/\d+')

    if [ -n "$ipv6_range" ]; then
        ipv6_range=$(get_ipv6_prefix "$ipv6_range")
        if [ -n "$ipv6_range" ] && validate_ipv6 "$ipv6_range"; then
            echo "Đã phát hiện dải IPv6: $ipv6_range"
            echo "$ipv6_range"
            return 0
        fi
    fi

    echo "Không tìm thấy dải IPv6 /64 trên giao diện $interface."
    echo "Vui lòng kiểm tra với nhà cung cấp VPS để lấy dải IPv6 /64 (ví dụ: 2401:2420:0:102f::/64)."
    while true; do
        echo "Nhập địa chỉ IPv6 đầy đủ (ví dụ: 2401:2420:0:102f:0000:0000:0000:0001/64):"
        read -r ipv6_input
        ipv6_range=$(get_ipv6_prefix "$ipv6_input")
        if [ -n "$ipv6_range" ] && validate_ipv6 "$ipv6_range"; then
            echo "Đã tách prefix IPv6: $ipv6_range"
            echo "$ipv6_range"
            return 0
        fi
    done
}

# Kiểm tra IPv6 có được kích hoạt không
check_ipv6_enabled

# Tự động phát hiện hoặc nhập dải IPv6
IPV6_RANGE=$(get_ipv6_range)
if [ $? -ne 0 ]; then
    echo "Lỗi: Không thể xác định dải IPv6!"
    exit 1
fi

# Tạo địa chỉ IPv6 hợp lệ (thêm ::1 vào cuối dải /64)
IPV6_BASE=$(python3 -c "import ipaddress; net=ipaddress.IPv6Network('$IPV6_RANGE', strict=True); print(net.network_address.compressed)")
IPV6_ADDRESS="${IPV6_BASE}::1/64"

echo "Địa chỉ IPv6 sẽ gán: $IPV6_ADDRESS"

# Gán địa chỉ IPv6 mặc định cho giao diện mạng
interface=$(ip link | grep '^[0-9]' | grep -v lo | awk -F': ' '{print $2}' | head -n 1)
if [ -z "$interface" ]; then
    echo "Lỗi: Không tìm thấy giao diện mạng!"
    exit 1
fi

# Xóa địa chỉ IPv6 cũ và gán mới
ip -6 addr flush dev "$interface"
ip -6 addr add "$IPV6_ADDRESS" dev "$interface"

if ip -6 addr show dev "$interface" | grep -q "${IPV6_BASE}"; then
    echo "Đã gán địa chỉ IPv6 $IPV6_ADDRESS vào $interface."
else
    echo "Lỗi: Không thể gán địa chỉ IPv6! Vui lòng kiểm tra dải IPv6 với nhà cung cấp VPS."
    exit 1
fi
