"""Place a test vendor call from the command line (the server must already be running).

  uv run python call.py +919876543210 --lang kn-IN --vendor "Annapurna Caterers"
"""

import argparse

from server import CallRequest, start_call


def main() -> None:
    parser = argparse.ArgumentParser(description="Place a Bandobast vendor call")
    parser.add_argument("to", help="Phone number in E.164, e.g. +919876543210")
    parser.add_argument("--lang", default="hi-IN", help="Vendor language, e.g. hi-IN, kn-IN")
    parser.add_argument("--vendor", default="Annapurna Caterers")
    parser.add_argument("--family", default="Sharma")
    parser.add_argument(
        "--say-test",
        action="store_true",
        help="Have Twilio speak a test line before starting the stream (debugging)",
    )
    args = parser.parse_args()

    sid = start_call(
        CallRequest(
            to=args.to,
            language=args.lang,
            vendor_name=args.vendor,
            family_name=args.family,
            say_test=args.say_test,
        )
    )
    print(f"Call started: {sid}")


if __name__ == "__main__":
    main()
