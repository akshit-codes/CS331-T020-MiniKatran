#!/usr/bin/env python3
"""Print a capture's packets one per line, with our encapsulation header (D7) spelled out.
tcpdump only shows it as a UDP payload. Usage: python3 decode.py file.pcap"""
import socket
import struct
import sys

FLAGS = {0x02: "SYN", 0x10: "ACK", 0x12: "SYN-ACK", 0x18: "PSH-ACK", 0x11: "FIN-ACK", 0x04: "RST"}


def ip(b):
    return socket.inet_ntoa(b)


def tcp(p):
    """p starts at an IPv4 header."""
    ihl = (p[0] & 15) * 4
    sport, dport = struct.unpack("!HH", p[ihl:ihl + 4])
    flags = FLAGS.get(p[ihl + 13], hex(p[ihl + 13]))
    return f"{ip(p[12:16])}:{sport} > {ip(p[16:20])}:{dport} {flags}"


data = open(sys.argv[1], "rb").read()
at, n, t0 = 24, 0, None
while at < len(data):
    sec, usec, caplen, _ = struct.unpack("<IIII", data[at:at + 16])
    pkt = data[at + 16:at + 16 + caplen]
    at += 16 + caplen
    n += 1
    now = sec + usec / 1e6
    t0 = t0 or now
    p = pkt[14:]
    if p[9] == 17:  # UDP: outer IP 20 + UDP 8, then the 12-byte header, then the original packet
        version, flags, bucket, second, gen = struct.unpack("!BBH4sI", p[28:40])
        print(f"{n:2} {now - t0:7.3f}s  outer {ip(p[12:16])} > {ip(p[16:20])} UDP {struct.unpack('!H', p[22:24])[0]} "
              f"| v{version} flags {flags:#04x}{' (HOPPED)' if flags & 1 else ''} bucket {bucket} second_hop {ip(second)} gen {gen} "
              f"| inner {tcp(p[40:])}")
    else:
        print(f"{n:2} {now - t0:7.3f}s  plain {tcp(p)}")
