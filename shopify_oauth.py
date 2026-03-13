#!/usr/bin/env python3
"""Quick Shopify OAuth flow to get an Admin API access token."""

import http.server
import json
import urllib.parse
import hashlib
import hmac
import requests

SHOP = "1bb2a2-2.myshopify.com"
CLIENT_ID = "REDACTED_CLIENT_ID"
CLIENT_SECRET = "REDACTED_CLIENT_SECRET"
SCOPES = "read_products,write_products,read_inventory,write_files"
REDIRECT_URI = "http://localhost:9999/callback"
PORT = 9999

auth_code = None


class OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/callback" and "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received")

    def log_message(self, format, *args):
        pass  # Silence logs


def main():
    # Step 1: Print auth URL for user to visit
    auth_url = (
        f"https://{SHOP}/admin/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={SCOPES}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    )

    print("\n" + "=" * 60)
    print("Open this URL in your browser:")
    print()
    print(auth_url)
    print()
    print("=" * 60)
    print("Waiting for authorization callback on localhost:9999...")

    # Step 2: Start local server to catch the callback
    server = http.server.HTTPServer(("localhost", PORT), OAuthHandler)
    while auth_code is None:
        server.handle_request()

    server.server_close()
    print(f"\nGot authorization code: {auth_code[:10]}...")

    # Step 3: Exchange code for access token
    print("Exchanging for access token...")
    resp = requests.post(
        f"https://{SHOP}/admin/oauth/access_token",
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": auth_code,
        },
        timeout=30,
    )

    if resp.status_code == 200:
        data = resp.json()
        token = data.get("access_token", "")
        print(f"\nAccess token: {token}")
        print(f"\nSave this! Run:")
        print(f'  export SHOPIFY_ACCESS_TOKEN="{token}"')

        # Save to a file for convenience
        with open("shopify_token.json", "w") as f:
            json.dump({"shop": SHOP, "access_token": token, "scope": data.get("scope", "")}, f, indent=2)
        print(f"\nAlso saved to shopify_token.json")
    else:
        print(f"\nError {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    main()
