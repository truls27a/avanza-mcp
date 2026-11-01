# avanza-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant read your
[Avanza](https://www.avanza.se) accounts, holdings and transactions, and look up public market data.
You log in with BankID through a small login panel shown right in the chat.

> **Unofficial.** Avanza has no public API. This server uses the same internal endpoints as
> avanza.se's own web app. They can change or break at any time, and automated use may be against
> Avanza's terms. Not affiliated with Avanza or BankID. Use at your own risk.

## What it can do

Read-only. There are no tools for orders or transfers.

| Tool | Login needed | What it returns |
|---|---|---|
| `connect_avanza` | | Shows the BankID login panel |
| `avanza_status` | | Whether you're logged in, and how old the snapshot is |
| `disconnect_avanza` | | Logs out and deletes the stored session |
| `get_accounts` | snapshot without login | Accounts with total value and cash |
| `get_holdings` | snapshot without login | Positions and cash, for one account or all |
| `get_transactions` | yes | Trades, deposits, dividends and fees |
| `search_instruments` | no | Stocks, funds, ETFs etc. by name, ticker or ISIN |
| `get_instrument` | no | Quote, key ratios, dividends, or fund details |
| `get_price_history` | no | Closing prices from one week to five years |

Avanza ends a web session after at most 24 hours. Every time you log in, the server saves a snapshot
of your accounts and holdings. While you're logged out, `get_accounts` and `get_holdings` answer from
that snapshot, with holdings re-priced from live public quotes.

## The login panel

`connect_avanza` returns an interactive [MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps)
panel instead of a link:

- **On a computer** it shows a live BankID QR code. Scan it with the BankID app.
- **On a phone** it shows an **Open BankID** button that opens the BankID app on the same device.

The login only starts when the panel asks for it. That matters because BankID cancels a login the
app hasn't picked up within about 30 seconds. A link written out by the assistant often arrives too
late; the panel avoids that. The QR code is refreshed every second, and a new login is started
automatically if nobody scans within 30 seconds.

You need a client that renders MCP Apps panels, such as ChatGPT. In a client without MCP Apps support
the market-data tools still work, but there is no way to log in.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/truls27a/avanza-mcp.git
cd avanza-mcp
uv sync
uv run avanza-mcp --help
```

### Local clients (stdio)

For a client that starts the server itself, add something like this to its MCP config:

```json
{
  "mcpServers": {
    "avanza": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/avanza-mcp", "avanza-mcp"]
    }
  }
}
```

### Remote clients (HTTP)

```sh
AVANZA_MCP_TOKEN="$(openssl rand -hex 32)" uv run avanza-mcp --transport http --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`, and `/healthz` is a health check.

- By default it listens on `127.0.0.1` only.
- If `AVANZA_MCP_TOKEN` is set, every request must send `Authorization: Bearer <token>`.
- It refuses to listen on a non-loopback address without a token. The exception is
  `--allow-unauthenticated`, meant for a private container network where the only way in is a tunnel
  you control.

ChatGPT needs to reach the server over the internet. Put it behind a tunnel or reverse proxy that
handles authentication, and never expose an unauthenticated port.

### Docker

```sh
docker build -t avanza-mcp .
docker run -d --name avanza-mcp -p 127.0.0.1:8000:8000 \
  -e AVANZA_MCP_TOKEN=change-me -v avanza-mcp-data:/data avanza-mcp
```

## Configuration

| Setting | Default | |
|---|---|---|
| `--transport` | `stdio` | `stdio` or `http` |
| `--host` / `--port` | `127.0.0.1` / `8000` | HTTP only |
| `--data-dir` / `AVANZA_MCP_DATA_DIR` | `~/.local/state/avanza-mcp` | Where the session and snapshot are stored |
| `AVANZA_MCP_TOKEN` | unset | Bearer token required on HTTP |
| `--allow-unauthenticated` | off | Allow a non-loopback HTTP address without a token |

## Security

Read this before you run it.

- **One user per server.** The server stores one Avanza session. Anyone who can call the server can
  read that person's accounts, holdings and transactions. Run your own instance and keep it private.
- **The stored session can do more than this server does.** The tools are read-only, but the stored
  cookies are a normal Avanza web session, which can also place orders. Anyone who gets hold of the
  data directory can act on the account until the session ends (at most 24 hours). The directory is
  created `0700` and the database `0600`. Use `disconnect_avanza` to log out early.
- **No credentials are stored.** Login is BankID only; the server never sees a password or a personal
  identity number.
- Instrument names and descriptions come from Avanza. The server tells the model to treat them as
  data, not instructions.

## Limitations

- If one BankID has several Avanza customer profiles (for example private plus a company), login
  fails: choosing a profile isn't supported yet.
- Endpoints and response shapes are reverse-engineered and may change without notice.

## License

[MIT](LICENSE)
