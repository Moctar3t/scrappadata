#!/bin/bash
# wake_gpu.sh
# ----------------------------------------------------
# Wakes up the Windows GPU PC wirelessly from your Mac
# MAC Address: A4-42-3B-6A-11-9D
# ----------------------------------------------------

echo "📡 Sending Magic Packet to wake up your Windows GPU PC..."

python3 -c '
import socket, struct
mac = "A4-42-3B-6A-11-9D"
data = "".join(mac.split(" "))
if len(data) != 12:
    data = "".join(mac.split(":"))
if len(data) != 12:
    data = "".join(mac.split("-"))
    
if len(data) == 12:
    packet = bytes.fromhex("F" * 12 + data * 16)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(packet, ("255.255.255.255", 9))
    print("✅ Magic Packet sent successfully to A4-42-3B-6A-11-9D!")
else:
    print("❌ Invalid MAC Address format: " + mac)
'
