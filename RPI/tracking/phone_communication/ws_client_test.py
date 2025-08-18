# ws_test_client.py
import asyncio, websockets, logging

async def test():
    uri = "ws://localhost:8765"
    async with websockets.connect(uri, max_size=10*1024*1024) as ws:
        print("connected")
        await ws.send("hello-from-pi-client")
        msg = await ws.recv()
        print("recv:", msg)

asyncio.run(test())
