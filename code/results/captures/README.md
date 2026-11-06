# Packet captures

Three short captures from one run on the namespace testbed (no tuning, 2026-11-06).
Decoded by `python3 decode.py FILE.pcap`, because tcpdump shows our 12-byte header
(D7) only as a UDP payload. Open the `.pcap` files in Wireshark or with `tcpdump -nn -X -r`.

Addresses: client 10.0.0.2, VIP 10.2.0.1, LB 10.1.0.10, beP1 .11, beP2 .12, beE1 .13, beE2 .14.

## 1. LB to backend: encapsulation (`1-encapsulated.pcap`, on the LB)

```
 1   0.000s  plain 10.0.0.2:40486 > 10.2.0.1:8080 SYN
 2   0.000s  outer 10.1.0.10 > 10.1.0.14 UDP 7700 | v1 flags 0x00 bucket 2773 second_hop 10.1.0.13 gen 2 | inner 10.0.0.2:40486 > 10.2.0.1:8080 SYN
```
What to look at: each packet appears twice, as it enters and as it leaves. The original
packet is unchanged inside (client to VIP), wrapped in IP + UDP + our 12 bytes, 40 in all.
The header carries the second hop, so the backend needs no table. The LB keeps no state:
packet 2 follows from the hash and the table alone. Packets 3–6 show the ACK and the request.

## 2. Second hop after a swap (`2-second-hop-forward.pcap`, on beP1)

```
 1   0.000s  outer 10.1.0.10 > 10.1.0.11 UDP 7700 | v1 flags 0x00 bucket 879 second_hop 10.1.0.12 gen 3 | inner 10.0.0.2:40568 > 10.2.0.1:8080 PSH-ACK
 2   0.000s  outer 10.1.0.11 > 10.1.0.12 UDP 7700 | v1 flags 0x01 (HOPPED) bucket 879 second_hop 10.1.0.12 gen 3 | inner 10.0.0.2:40568 > 10.2.0.1:8080 PSH-ACK
```
What to look at: the connection opened on beP2, then the bucket's hops were exchanged
(`[beP2|beP1]` to `[beP1|beP2]`; all 2047 changed buckets were exact swaps in this run).
So the LB now sends it to beP1 (packet 1), which has no socket for it and forwards it once
to beP2 (packet 2). Only the outer addresses and the HOPPED flag change, and the flag stops
a second forward. The client sees nothing. Capture 3 shows this connection's replies.

## 3. Direct reply that skips the LB (`3-direct-reply.pcap`, on beP2)

```
 1   0.000s  plain 10.2.0.1:8080 > 10.0.0.2:40558 PSH-ACK
 2   0.030s  plain 10.2.0.1:8080 > 10.0.0.2:40568 PSH-ACK
```
What to look at: the backend answers from the VIP straight to the client, with no
encapsulation. Port 40568 is the same connection as in capture 2, so even a request that
took two hops gets a one-hop reply. While this was captured the LB saw 312 packets, and **0**
had the VIP as source (not saved: a separate 3-s capture on the LB, filter `src host 10.2.0.1`).

## How they were taken

`tcpdump -c 6 -i eth0` (`-c 4` for the third) inside the namespaces, with connections from
`loadgen -conns 16 -rate 40` and `curl`. Tables: `lbctl table equal`, then
`lbctl table beP1=0,beP2=1,beE1=1,beE2=1`, connections opened, then `lbctl table equal` again.
The filters: `host 10.2.0.1 or udp port 7700`; `udp port 7700`; `tcp and src host 10.2.0.1`.
