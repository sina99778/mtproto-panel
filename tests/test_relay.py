"""Functional test for the tunneled-mode TCP relay:  python tests/test_relay.py

Spins up an echo "upstream", points relay._handle at it, connects through the
relay and verifies byte-perfect bidirectional forwarding plus clean teardown.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import relay  # noqa: E402


async def echo(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def main():
    echo_srv = await asyncio.start_server(echo, "127.0.0.1", 0)
    eport = echo_srv.sockets[0].getsockname()[1]
    relay_srv = await asyncio.start_server(
        lambda r, w: relay._handle(r, w, "127.0.0.1", eport), "127.0.0.1", 0
    )
    rport = relay_srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", rport)
    writer.write(b"hello mtproto")
    await writer.drain()
    data = await asyncio.wait_for(reader.read(1024), timeout=5)
    assert data == b"hello mtproto", data
    print("  ok  relay forwards + echoes bytes through upstream")

    payload = b"x" * 200_000
    writer.write(payload)
    await writer.drain()
    got = b""
    while len(got) < len(payload):
        chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not chunk:
            break
        got += chunk
    assert got == payload, f"got {len(got)} of {len(payload)} bytes"
    print("  ok  relay handles 200KB bidirectional payload")

    writer.close()
    await asyncio.sleep(0.2)  # let teardown propagate
    echo_srv.close()
    relay_srv.close()
    await echo_srv.wait_closed()
    await relay_srv.wait_closed()
    print("  ok  relay tears down cleanly on client close (no hang)")


asyncio.run(main())
print("\n=== RELAY TESTS PASSED ===")
