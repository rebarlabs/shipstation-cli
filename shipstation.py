#!/usr/bin/env python3
"""
ShipStation CLI - Fetch current orders from ShipStation API
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


SHIPSTATION_API_URL = "https://ssapi.shipstation.com"
DEFAULT_DB_PATH = Path.home() / ".shipstation" / "orders.db"


def send_slack_message(token: str, channel: str, order: dict, store_map: dict = None) -> bool:
    """Send an order notification to Slack using the API."""
    order_number = order.get("orderNumber", "N/A")
    store_id = order.get("advancedOptions", {}).get("storeId")
    store_name = store_map.get(store_id, "Unknown") if store_map else "Unknown"

    customer = order.get("shipTo", {})
    customer_name = customer.get("name", "N/A")
    street = customer.get("street1", "")
    city = customer.get("city", "").title()
    state = customer.get("state", "")
    postal = customer.get("postalCode", "")
    country = customer.get("country", "")

    location_display = ", ".join(p for p in [city, state, country] if p)
    full_address = ", ".join(p for p in [street, city, state, postal, country] if p)
    maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(full_address)}"
    location = f"<{maps_url}|{location_display}>"

    total = order.get("orderTotal", 0)
    items = order.get("items", [])
    items_lines = "\n".join(
        f"{item.get('quantity', 1)}x {item.get('name', 'Item')}"
        for item in items
    )

    payload = {
        "channel": channel,
        "unfurl_links": False,
        "unfurl_media": False,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📦 *#{order_number}* · {store_name} · ${total:.2f}\n\n"
                            f"{customer_name}\n"
                            f"{location}\n\n"
                            f"{items_lines}"
                }
            },
            {
                "type": "divider"
            }
        ]
    }

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    request = Request("https://slack.com/api/chat.postMessage", data=data, headers=headers)

    try:
        with urlopen(request) as response:
            result = json.loads(response.read().decode())
            if not result.get("ok"):
                print(f"Slack API error: {result.get('error')}", file=sys.stderr)
                return False
            return True
    except (HTTPError, URLError) as e:
        print(f"Error sending to Slack: {e}", file=sys.stderr)
        return False


def get_db_connection(db_path: Path = None) -> sqlite3.Connection:
    """Get a connection to the SQLite database, creating it if needed."""
    db_path = db_path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_orders (
            order_id INTEGER PRIMARY KEY,
            order_number TEXT,
            first_seen_at TEXT,
            order_date TEXT,
            store_id INTEGER,
            customer_name TEXT,
            order_total REAL
        )
    """)
    conn.commit()
    return conn


def get_seen_order_ids(conn: sqlite3.Connection) -> set:
    """Get set of all previously seen order IDs."""
    cursor = conn.execute("SELECT order_id FROM seen_orders")
    return {row[0] for row in cursor.fetchall()}


def mark_orders_seen(conn: sqlite3.Connection, orders: list) -> None:
    """Mark orders as seen in the database."""
    now = datetime.now().isoformat()
    for order in orders:
        conn.execute("""
            INSERT OR IGNORE INTO seen_orders
            (order_id, order_number, first_seen_at, order_date, store_id, customer_name, order_total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            order.get("orderId"),
            order.get("orderNumber"),
            now,
            order.get("orderDate"),
            order.get("advancedOptions", {}).get("storeId"),
            order.get("shipTo", {}).get("name"),
            order.get("orderTotal", 0)
        ))
    conn.commit()


def api_request(url: str, api_key: str, api_secret: str) -> dict:
    """Make an authenticated request to ShipStation API."""
    headers = {
        "Authorization": get_auth_header(api_key, api_secret),
        "Content-Type": "application/json",
    }
    request = Request(url, headers=headers)

    try:
        with urlopen(request) as response:
            return json.loads(response.read().decode())
    except HTTPError as e:
        if e.code == 401:
            print("Error: Invalid API credentials", file=sys.stderr)
        elif e.code == 429:
            print("Error: Rate limit exceeded. Please wait and try again.", file=sys.stderr)
        else:
            print(f"Error: HTTP {e.code} - {e.reason}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Error: Unable to connect to ShipStation API - {e.reason}", file=sys.stderr)
        sys.exit(1)


def get_auth_header(api_key: str, api_secret: str) -> str:
    """Create Basic Auth header from API credentials."""
    credentials = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def fetch_stores(api_key: str, api_secret: str) -> dict:
    """Fetch stores from ShipStation API. Returns dict mapping store ID to store name."""
    url = f"{SHIPSTATION_API_URL}/stores"
    stores = api_request(url, api_key, api_secret)
    return {store["storeId"]: store["storeName"] for store in stores}


def fetch_orders(api_key: str, api_secret: str, status: str = None,
                 store_id: int = None, debug: bool = False) -> list:
    """Fetch all orders from ShipStation API, handling pagination."""
    base_params = ["pageSize=500", "sortBy=CreateDate", "sortDir=DESC"]

    if status:
        base_params.append(f"orderStatus={status}")

    if store_id:
        base_params.append(f"storeId={store_id}")

    all_orders = []
    page = 1

    while True:
        params = base_params + [f"page={page}"]
        url = f"{SHIPSTATION_API_URL}/orders?" + "&".join(params)
        result = api_request(url, api_key, api_secret)

        orders = result.get("orders", [])
        all_orders.extend(orders)

        total = result.get("total", 0)
        pages = result.get("pages", 1)

        if debug:
            print(f"[DEBUG] Page {page}/{pages}: fetched {len(orders)} orders (total: {total})", file=sys.stderr)

        if page >= pages:
            break
        page += 1

    return all_orders


def format_order(order: dict, verbose: bool = False, is_new: bool = False) -> str:
    """Format a single order for display."""
    order_id = order.get("orderId", "N/A")
    order_number = order.get("orderNumber", "N/A")
    status = order.get("orderStatus", "N/A")
    order_date = order.get("orderDate", "N/A")
    if order_date and order_date != "N/A":
        order_date = order_date.split("T")[0]

    customer = order.get("shipTo", {})
    customer_name = customer.get("name", "N/A")

    total = order.get("orderTotal", 0)
    items_count = len(order.get("items", []))

    new_marker = "[NEW] " if is_new else ""
    output = f"{new_marker}#{order_number} | {status} | {order_date} | {customer_name} | ${total:.2f} | {items_count} item(s)"

    if verbose:
        output += "\n  Items:"
        for item in order.get("items", []):
            sku = item.get("sku", "N/A")
            name = item.get("name", "N/A")
            qty = item.get("quantity", 0)
            output += f"\n    - [{sku}] {name} x{qty}"

        shipping = order.get("requestedShippingService", "N/A")
        output += f"\n  Shipping: {shipping}"

        address = customer
        if address:
            addr_parts = [
                address.get("street1", ""),
                address.get("street2", ""),
                f"{address.get('city', '')}, {address.get('state', '')} {address.get('postalCode', '')}",
                address.get("country", "")
            ]
            addr_str = ", ".join(p for p in addr_parts if p.strip())
            output += f"\n  Ship To: {addr_str}"

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Fetch current orders from ShipStation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  SHIPSTATION_API_KEY      Your ShipStation API key
  SHIPSTATION_API_SECRET   Your ShipStation API secret
  SLACK_BOT_TOKEN          Slack bot token (for --slack)
  SLACK_CHANNEL            Slack channel ID (for --slack)

Examples:
  %(prog)s --stores "My Store"              Fetch unshipped orders from a store
  %(prog)s --stores "My Store" --country US Filter by store and country
  %(prog)s --stores "My Store" --new-only   Show only new orders since last run
  %(prog)s --order 12345                    Fetch a specific order by number
  %(prog)s --list-stores                    List available stores
        """
    )

    parser.add_argument(
        "--status", "-s",
        choices=["awaiting_payment", "awaiting_shipment", "pending_fulfillment",
                 "shipped", "on_hold", "cancelled", "all"],
        default="awaiting_shipment",
        help="Filter by order status (default: awaiting_shipment, use 'all' for no filter)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed order information"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output raw JSON response"
    )
    parser.add_argument(
        "--stores",
        help="Comma-separated list of store names to filter by"
    )
    parser.add_argument(
        "--country",
        help="Filter by shipping country code (e.g., US, CA, GB)"
    )
    parser.add_argument(
        "--new-only", "-n",
        action="store_true",
        help="Only show orders not seen in previous runs"
    )
    parser.add_argument(
        "--order",
        help="Fetch a specific order by order number (for debugging)"
    )
    parser.add_argument(
        "--list-stores",
        action="store_true",
        help="List all stores and their IDs"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show debug info for filtering"
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help="Send each order to Slack (requires SLACK_BOT_TOKEN and SLACK_CHANNEL env vars)"
    )

    args = parser.parse_args()

    # Show help if no arguments provided
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    api_key = os.environ.get("SHIPSTATION_API_KEY")
    api_secret = os.environ.get("SHIPSTATION_API_SECRET")

    if not api_key or not api_secret:
        print("Error: API credentials required.", file=sys.stderr)
        print("Set SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET environment variables.", file=sys.stderr)
        sys.exit(1)

    # Validate Slack config if --slack is used
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")
    if args.slack and (not slack_token or not slack_channel):
        print("Error: --slack requires SLACK_BOT_TOKEN and SLACK_CHANNEL environment variables.", file=sys.stderr)
        sys.exit(1)

    # List stores
    if args.list_stores:
        store_map = fetch_stores(api_key, api_secret)
        print("Available stores:")
        print("-" * 50)
        for store_id, store_name in sorted(store_map.items(), key=lambda x: x[1]):
            print(f"  {store_id}: {store_name}")
        return

    # Fetch specific order by order number
    if args.order:
        url = f"{SHIPSTATION_API_URL}/orders?orderNumber={args.order}"
        result = api_request(url, api_key, api_secret)
        orders = result.get("orders", [])
        if not orders:
            print(f"Order {args.order} not found.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(orders[0], indent=2))
        return

    status = None if args.status == "all" else args.status

    # Resolve store names to IDs for server-side filtering
    store_ids = []
    store_map = {}
    if args.stores or args.slack:
        store_map = fetch_stores(api_key, api_secret)
    if args.stores:
        # Create reverse map: name -> id
        name_to_id = {name.lower(): sid for sid, name in store_map.items()}
        store_names = [s.strip().lower() for s in args.stores.split(",")]
        for name in store_names:
            if name in name_to_id:
                store_ids.append(name_to_id[name])
            else:
                print(f"Warning: Store '{name}' not found. Use --list-stores to see available stores.", file=sys.stderr)
        if args.debug:
            print(f"[DEBUG] Store names: {store_names} -> Store IDs: {store_ids}", file=sys.stderr)

    # Fetch orders (with server-side store filtering)
    if args.debug:
        print(f"[DEBUG] Fetching orders: status={status}", file=sys.stderr)

    orders = []
    if len(store_ids) == 1:
        # Single store
        orders = fetch_orders(api_key, api_secret, status, store_id=store_ids[0], debug=args.debug)
    elif len(store_ids) > 1:
        # Multiple stores - make separate API calls
        for sid in store_ids:
            store_orders = fetch_orders(api_key, api_secret, status, store_id=sid, debug=args.debug)
            orders.extend(store_orders)
        # Sort combined results by create date descending
        orders.sort(key=lambda o: o.get("createDate", ""), reverse=True)
    else:
        # No store filter
        orders = fetch_orders(api_key, api_secret, status, debug=args.debug)

    # Filter by country if specified
    if args.country:
        country = args.country.upper()
        if args.debug:
            print(f"[DEBUG] Filtering by country: {country}", file=sys.stderr)
        before_count = len(orders)
        filtered_orders = []
        for order in orders:
            order_country = order.get("shipTo", {}).get("country", "").upper()
            if order_country == country:
                filtered_orders.append(order)
            elif args.debug:
                print(f"[DEBUG] Order #{order.get('orderNumber')} excluded: country='{order_country}'", file=sys.stderr)
        orders = filtered_orders
        if args.debug:
            print(f"[DEBUG] Country filter: {before_count} -> {len(orders)} orders", file=sys.stderr)

    # Check for new orders using database
    conn = get_db_connection()
    seen_ids = get_seen_order_ids(conn)
    new_order_ids = set()

    for order in orders:
        order_id = order.get("orderId")
        if order_id not in seen_ids:
            new_order_ids.add(order_id)

    # Filter to new only if requested
    if args.new_only:
        orders = [o for o in orders if o.get("orderId") in new_order_ids]

    if args.json:
        for order in orders:
            order["_isNew"] = order.get("orderId") in new_order_ids
        print(json.dumps({"orders": orders, "total": len(orders)}, indent=2))
        mark_orders_seen(conn, orders)
        conn.close()
        return

    new_count = len([o for o in orders if o.get("orderId") in new_order_ids])
    print(f"Found {len(orders)} order(s)" + (f" ({new_count} new)" if new_count else ""))
    print("-" * 80)

    if not orders:
        print("No orders found.")
        conn.close()
        return

    slack_count = 0
    for order in orders:
        is_new = order.get("orderId") in new_order_ids
        print(format_order(order, args.verbose, is_new))
        if args.verbose:
            print()
        if args.slack:
            if send_slack_message(slack_token, slack_channel, order, store_map):
                slack_count += 1

    if args.slack:
        print(f"Sent {slack_count} order(s) to Slack")

    # Mark all displayed orders as seen
    mark_orders_seen(conn, orders)
    conn.close()


if __name__ == "__main__":
    main()
