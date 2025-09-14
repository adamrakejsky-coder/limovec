import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
TOKEN = os.environ['TOKEN']

# Cache nastavení
CACHE_CONFIG = {
    'audit_cache_size': 1000,
    'guild_cache_size': 500,
    'default_expiry': 3600
}

# Rate limiting nastavení
RATE_LIMITS = {
    'audit_log': {'max_calls': 5, 'window': 60},
    'reactions': {'max_calls': 20, 'window': 60},
    'voice_debounce': 5,
    'ticket_creation': {'max_calls': 1, 'window': 300}  # 1 ticket za 5 minut
}

# Ticket systém konfigurace
TICKET_CONFIG = {
    'max_active_tickets_per_user': 3,
    'transcript_retention_days': 30,
    'auto_close_inactive_hours': 72
}