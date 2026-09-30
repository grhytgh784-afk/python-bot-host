# Python Bot Hosting — Railway

## Start command
```bash
gunicorn --bind 0.0.0.0:$PORT app:app
```

## API
POST /api/upload
POST /api/bots/upload
GET /api/bots
GET /api/bots/{id}/stats
POST /api/bots/{id}/start
POST /api/bots/{id}/stop
POST /api/bots/{id}/restart
DELETE /api/bots/{id}
GET /api/bots/{id}/logs
GET /api/info
GET /api/stats
GET /health

CORS is enabled with origins="*".

The upload endpoint accepts both `file` and `bot_file` so it is compatible with the current local HTML dashboard.

Important: Railway service storage can be ephemeral. For durable bot files/logs, attach persistent storage or use external storage.
