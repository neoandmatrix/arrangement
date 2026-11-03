"""Simulate a phone call without a phone: speak scripted lines into the bot as if over Twilio.

The other side's lines are synthesised with Gnani Timbre (8 kHz mu-law, like a real call) and
streamed into the server's /ws at real-time pace; the bot answers through the normal pipeline.

  # terminal 1 (a throwaway database keeps your dashboard data clean)
  BANDOBAST_DB=/tmp/sim.db uv run uvicorn server:app --port 8799
  # terminal 2
  BANDOBAST_DB=/tmp/sim.db uv run python simulate.py --port 8799 --mode vendor_call --vendor C02

Uses Gnani and Groq credits. Prints what each side said, then the saved quote or check-in.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import websockets
from dotenv import load_dotenv
from gnani.tts import AudioConfig, GnaniTTSClient

import prompts
import store

FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law
SILENCE = b"\xff" * FRAME_BYTES

SCRIPTS = {
    "vendor_call": [
        "हाँ जी, बोलिए, अभी बात कर सकते हैं।",
        "हाँ, पंद्रह तारीख़ को सुबह हम फ्री हैं। बिना प्याज़ लहसुन का खाना भी बना देंगे।",
        "चालीस लोगों के लिए चार सौ अस्सी रुपये प्रति प्लेट लगेगा।",
        "ट्रांसपोर्ट का एक हज़ार रुपये अलग से लगेगा। GST शामिल है।",
        "पाँच हज़ार रुपये एडवांस देना होगा।",
        "हाँ जी, बिल्कुल सही है।",
        "पूरा ट्रांसपोर्ट तो नहीं, चलिए आधा कर देते हैं, पाँच सौ रुपये।",
        "हाँ जी, सही है।",
        "ठीक है जी, धन्यवाद।",
    ],
    # With --tight-budget (caterer budget ₹18,000): the bot should push back twice, then accept.
    "vendor_call_negotiate": [
        "हाँ जी, बोलिए, अभी बात कर सकते हैं।",
        "हाँ, पंद्रह तारीख़ को सुबह हम फ्री हैं। बिना प्याज़ लहसुन का खाना भी बना देंगे।",
        "चालीस लोगों के लिए चार सौ अस्सी रुपये प्रति प्लेट लगेगा।",
        "ट्रांसपोर्ट का एक हज़ार रुपये अलग से लगेगा। GST शामिल है।",
        "पाँच हज़ार रुपये एडवांस देना होगा।",
        "हाँ जी, बिल्कुल सही है।",
        "चार सौ बीस में तो मुश्किल है जी, चलिए चार सौ पचास कर देते हैं।",
        "नहीं जी, चार सौ पचास से कम नहीं हो पाएगा।",
        "हाँ जी, सही है।",
        "ठीक है जी, धन्यवाद।",
    ],
    "checkin": [
        "हाँ जी, बोलिए।",
        "माफ़ कीजिए, गाड़ी ख़राब हो गई है। हम सुबह सात बजकर पंद्रह मिनट तक पहुँचेंगे।",
        "हाँ, सात पंद्रह तक पक्का।",
        "ठीक है, धन्यवाद।",
    ],
    "intake": [
        "नमस्ते, हमें बेंगलुरु में गृह प्रवेश करवाना है।",
        "हम शर्मा परिवार हैं, मेरा नाम राकेश शर्मा है।",
        "एचएसआर लेआउट, बेंगलुरु में।",
        "पंद्रह नवंबर को, मुहूर्त सुबह साढ़े सात बजे है।",
        "लगभग चालीस लोग होंगे।",
        "उत्तर भारतीय शाकाहारी खाना, पूजा के लिए बिना प्याज़ लहसुन के।",
        "उत्तर भारतीय विधि से, हिंदी में पूजा होनी चाहिए।",
        "पंडित जी, खाना और सजावट, तीनों चाहिए।",
        "पंडित जी के लिए पंद्रह हज़ार, खाने के लिए पच्चीस हज़ार, सजावट के लिए बारह हज़ार।",
        "हाँ, सब सही है।",
        "धन्यवाद।",
    ],
}


CACHE = Path(__file__).parent / ".sim_cache"


def synthesize(tts: GnaniTTSClient, text: str) -> bytes:
    """Cached, so re-running a script doesn't spend TTS credits again."""
    path = CACHE / f"{hashlib.sha1(text.encode()).hexdigest()[:16]}.ulaw"
    if path.exists():
        return path.read_bytes()
    audio = tts.synthesize(
        text,
        voice="Avinash",
        model="timbre-v3.0",
        language="hi-IN",
        audio_config=AudioConfig(sample_rate=8000, container="mulaw"),
    )
    CACHE.mkdir(exist_ok=True)
    path.write_bytes(audio)
    return audio


async def run(args) -> None:
    load_dotenv(Path(__file__).parent / ".env")
    tts = GnaniTTSClient(api_key=os.environ["GNANI_API_KEY"])
    lines = SCRIPTS[args.script or args.mode]
    print(f"Synthesising {len(lines)} lines…")
    # One at a time: the Gnani account allows a single active request.
    audio = [await asyncio.to_thread(synthesize, tts, t) for t in lines]

    call_sid = f"CAsim{uuid.uuid4().hex[:10]}"
    params = {"mode": args.mode, "vendor_id": args.vendor or "", "checkin_id": str(args.checkin or "")}
    bot_last_audio = 0.0
    bot_started = asyncio.Event()

    async with websockets.connect(f"ws://localhost:{args.port}/ws") as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "streamSid": "MZsim",
                    "start": {
                        "streamSid": "MZsim",
                        "callSid": call_sid,
                        "accountSid": "ACsim",
                        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                        "customParameters": params,
                    },
                }
            )
        )

        async def receive():
            nonlocal bot_last_audio
            async for message in ws:
                if json.loads(message).get("event") == "media":
                    bot_last_audio = time.monotonic()
                    bot_started.set()

        async def send_frames(chunk: bytes):
            for i in range(0, len(chunk), FRAME_BYTES):
                frame = chunk[i : i + FRAME_BYTES].ljust(FRAME_BYTES, b"\xff")
                payload = base64.b64encode(frame).decode()
                await ws.send(json.dumps({"event": "media", "streamSid": "MZsim", "media": {"payload": payload}}))
                await asyncio.sleep(0.02)

        async def keep_line_open():
            # A real call carries audio continuously; silence between turns keeps VAD honest.
            while True:
                await send_frames(SILENCE)

        receiver = asyncio.create_task(receive())
        background = asyncio.create_task(keep_line_open())
        try:
            for line, clip in zip(lines, audio):
                # Wait for the bot to speak, then for 1.2 s of silence from it.
                await asyncio.wait_for(bot_started.wait(), 30)
                while time.monotonic() - bot_last_audio < 1.2:
                    await asyncio.sleep(0.1)
                bot_started.clear()
                print(f"  them: {line}")
                background.cancel()
                await send_frames(clip)
                background = asyncio.create_task(keep_line_open())
            await asyncio.wait_for(receiver, 40)  # the bot hangs up
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            background.cancel()
            receiver.cancel()

    call = None
    for _ in range(20):  # the server saves the call once its pipeline has shut down
        await asyncio.sleep(0.5)
        if call := next((c for c in store.calls(None) if c["id"] == call_sid), None):
            break
    if not call:
        print("No call record saved; check the server log.")
        return
    print(f"\nCall {call_sid}: {call['duration_secs']}s, end: {call['end_reason']}, latencies: {call['latencies']}")
    for t in call["transcript"]:
        print(f"  {'bot ' if t['role'] == 'bandobast' else 'them'}: {t['text']}")
    event = store.current_event()
    if args.mode == "vendor_call" and event:
        print("\nSaved quote:", store.latest_quotes(event[0]).get(args.vendor))
    if args.mode == "checkin" and event:
        print("\nCheck-ins:", [(c["label"], c["status"], c["eta"]) for c in store.checkins(event[0])])
        print("Alerts:", [a["message"] for a in store.alerts(event[0])])
    if args.mode == "intake" and event:
        print("\nSaved brief:", event[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a Bandobast call without a phone")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--mode", choices=["vendor_call", "checkin", "intake"], default="vendor_call")
    parser.add_argument("--script", choices=list(SCRIPTS), help="Which scripted lines to speak (default: the mode's)")
    parser.add_argument("--tight-budget", action="store_true", help="With --sample-brief: caterer budget ₹18,000")
    parser.add_argument("--vendor", default="C02", help="Vendor id (vendor_call)")
    parser.add_argument("--checkin", type=int, help="Check-in id (checkin)")
    parser.add_argument("--sample-brief", action="store_true", help="Load the sample brief first")
    args = parser.parse_args()
    if args.sample_brief:
        brief = prompts.SAMPLE_BRIEF
        if args.tight_budget:
            brief = {**brief, "budget": {**brief["budget"], "caterer": 18000}}
        store.save_event(brief)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
