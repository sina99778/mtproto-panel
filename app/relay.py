#!/usr/bin/env python3
"""High-performance, dependency-free TCP relay used by the "tunneled" mode.

Runs on the Iran-facing server: clients connect to this box (low domestic
latency) and every connection is forwarded transparently to the upstream
MTProto proxy abroad. FakeTLS is preserved end-to-end because we only move
raw bytes. Combined with BBR + sysctl tuning this gives the lowest practical
latency and packet loss.

Usage:
    python3 relay.py --listen 8443 --upstream-host 1.2.3.4 --upstream-port 443
"""
import argparse
import asyncio
import socket

try:  # uvloop dramatically lowers latency under load; optional.
    import uvloop  # type: ignore

    uvloop.install()
except Exception:
    pass

BUF = 65536


def _tune_socket(writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(BUF)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_reader, client_writer, upstream_host, upstream_port):
    try:
        up_reader, up_writer = await asyncio.open_connection(upstream_host, upstream_port)
    except OSError:
        try:
            client_writer.close()
        except Exception:
            pass
        return

    _tune_socket(client_writer)
    _tune_socket(up_writer)

    t1 = asyncio.ensure_future(_pipe(client_reader, up_writer))
    t2 = asyncio.ensure_future(_pipe(up_reader, client_writer))
    try:
        # As soon as EITHER direction ends, tear the whole session down so we
        # never leak a half-open socket (important under heavy churn).
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in (up_writer, client_writer):
            try:
                w.close()
            except Exception:
                pass
        await asyncio.gather(t1, t2, return_exceptions=True)


async def _main(listen, upstream_host, upstream_port):
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, upstream_host, upstream_port),
        host="0.0.0.0",
        port=listen,
        reuse_address=True,
        backlog=4096,
    )
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--upstream-host", required=True)
    ap.add_argument("--upstream-port", type=int, required=True)
    args = ap.parse_args()
    asyncio.run(_main(args.listen, args.upstream_host, args.upstream_port))


if __name__ == "__main__":
    main()
