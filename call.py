"""Place a phone call from the command line (the server and tunnel must already be running).

  uv run python call.py +919876543210 --vendor C01                 # enquiry call to a caterer
  uv run python call.py +919876543210 --mode checkin --checkin 3   # follow-up call
  uv run python call.py +919876543210 --mode intake                # you play the family
"""

import argparse

from server import CallRequest, start_call


def main() -> None:
    parser = argparse.ArgumentParser(description="Place a Bandobast phone call")
    parser.add_argument("to", help="Phone number in E.164, e.g. +919876543210")
    parser.add_argument("--mode", default="vendor_call", choices=["vendor_call", "checkin", "intake"])
    parser.add_argument("--vendor", default="C01", help="Vendor id from data/vendors.json")
    parser.add_argument("--checkin", type=int, help="Check-in id (for --mode checkin)")
    parser.add_argument(
        "--say-test",
        action="store_true",
        help="Have Twilio speak a test line before starting the stream (debugging)",
    )
    args = parser.parse_args()

    sid = start_call(
        CallRequest(
            to=args.to,
            mode=args.mode,
            vendor_id=args.vendor if args.mode == "vendor_call" else None,
            checkin_id=args.checkin,
            say_test=args.say_test,
        )
    )
    print(f"Call started: {sid}")


if __name__ == "__main__":
    main()
