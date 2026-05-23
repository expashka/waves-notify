# waves-notify

Universal lead notification service. Receives form submissions via HTTP and forwards them to **Telegram** and/or **Email**. Both channels are optional and independent.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lead` | Receive a lead from any form |
| `POST` | `/notify` | Send arbitrary text notification (requires `NOTIFY_SECRET` token) |
| `GET` | `/health` | Service health |

### POST /lead

```json
{
  "name":    "Ivan",
  "contact": "+7 900 000 00 00",
  "email":   "ivan@example.com",
  "message": "Optional message",
  "consent": true,
  "page":    "https://example.com/landing"
}
```

Only `consent: true` is required. At least one of `name`, `contact`, or `email` must be present.

### POST /notify

```http
POST /notify
X-Notify-Token: <NOTIFY_SECRET>
Content-Type: application/json

{ "text": "Deploy finished ✓" }
```

Optionally send to a specific Telegram chat:
```json
{ "text": "hello", "chat_id": "123456789" }
```

## Quick start

```bash
cp .env.example .env
# fill in TG_BOT_TOKEN and/or SMTP_* in .env
docker compose up -d
```

## Connecting a form

From your frontend, send a `POST` request to `/lead`:

```js
await fetch('http://your-server:8080/lead', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ name, contact, email, message, consent: true, page: location.href }),
})
```

## Telegram bot

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token to `TG_BOT_TOKEN`
2. Get your `chat_id`: start the bot and send `/chat_id`
3. Set `TG_ADMIN_CHAT_ID` to your chat_id

Admin commands: `/notify_list`, `/add_notify <id>`, `/remove_notify <id>`

## Environment variables

See [`.env.example`](.env.example) for all options.
