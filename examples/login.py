#!/usr/bin/env python3
"""Phone-OTP login: obtain an access token (JWT) for the client.

    python examples/login.py 989123456789      # full international, no '+'

Then export the printed token so the other examples can use it:

    export BALE_TOKEN="eyJ..."
"""
import sys

from bale import auth


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python examples/login.py <phone_int>   e.g. 989123456789")
        sys.exit(2)
    phone = int(sys.argv[1])

    print(f"Requesting a code for {phone} ...")
    session = auth.start_phone_auth(phone)
    print(f"  registered={session.is_registered} sentCodeType={session.sent_code_type} "
          f"(1=SMS, 2=call, 3=app, 0=none)")
    if session.sent_code_type == 0:
        print("  WARNING: server did not send a code (rate-limited / invalid phone / etc.)")

    code = input("Enter the OTP code: ").strip()
    result = auth.validate_code(session.transaction_hash, code)

    print(f"\nLogged in as {result.user_name!r} (id={result.user_id}).")
    print("Set this token in your environment to use the other examples:\n")
    print(f'  export BALE_TOKEN="{result.access_token}"')


if __name__ == "__main__":
    main()
