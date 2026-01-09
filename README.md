# ShipStation CLI

Command line tool to fetch orders from ShipStation and optionally send notifications to Slack.

## Setup

1. Get your API credentials from ShipStation:
   - Go to Settings > Account > API Settings
   - Generate or copy your API Key and API Secret

2. Set environment variables:
   ```bash
   export SHIPSTATION_API_KEY="your_api_key"
   export SHIPSTATION_API_SECRET="your_api_secret"
   ```

3. (Optional) For Slack notifications:
   ```bash
   export SLACK_BOT_TOKEN="xoxb-your-bot-token"
   export SLACK_CHANNEL="C1234567890"
   ```

4. Make the script executable:
   ```bash
   chmod +x shipstation.py
   ```

## Usage

```bash
# List available stores
./shipstation.py --list-stores

# Fetch unshipped orders from a store
./shipstation.py --stores "My Store"

# Filter by store and country
./shipstation.py --stores "My Store" --country US

# Show only new orders (not seen in previous runs)
./shipstation.py --stores "My Store" --new-only

# Send new orders to Slack
./shipstation.py --stores "My Store" --new-only --slack

# Fetch a specific order by number
./shipstation.py --order 12345

# Output as JSON
./shipstation.py --stores "My Store" --json

# Show detailed order information
./shipstation.py --stores "My Store" --verbose
```

## Options

| Option | Description |
|--------|-------------|
| `--stores` | Comma-separated list of store names to filter by |
| `--country` | Filter by shipping country code (e.g., US, CA, GB) |
| `-s, --status` | Filter by status (default: awaiting_shipment) |
| `-n, --new-only` | Only show orders not seen in previous runs |
| `--order` | Fetch a specific order by order number |
| `--list-stores` | List all available stores |
| `--slack` | Send each order to Slack |
| `-v, --verbose` | Show detailed order information |
| `-j, --json` | Output raw JSON response |
| `--debug` | Show debug info for filtering |

## Cron Usage

To check for new orders periodically and send Slack notifications:

```bash
*/15 * * * * SHIPSTATION_API_KEY="xxx" SHIPSTATION_API_SECRET="xxx" SLACK_BOT_TOKEN="xxx" SLACK_CHANNEL="xxx" /usr/bin/python3 /path/to/shipstation.py --stores "My Store" --new-only --slack > /dev/null 2>&1
```

Or with an env file:

```bash
*/15 * * * * . /path/to/.env && /usr/bin/python3 /path/to/shipstation.py --stores "My Store" --new-only --slack > /dev/null
```

## Order Tracking

The script maintains a SQLite database at `~/.shipstation/orders.db` to track which orders have been seen. This enables the `--new-only` flag to only show/notify on orders that haven't been processed before.
