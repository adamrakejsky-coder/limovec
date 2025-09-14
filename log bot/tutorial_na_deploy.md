# 🚀 Deployment Guide - Integrovaný Discord Bot

## ✅ Co bylo dokončeno

### 1. **Modularizace**
- ✅ Vytvořena struktura `bot/` s podsložkami
- ✅ Refactorovaná konfigurace do `bot/config/settings.py`
- ✅ Database manager s asyncpg connection pooling
- ✅ LRU cache systém s expirací
- ✅ Rate limiting utilities

### 2. **Ticket Systém**
- ✅ Async ticket database handler s cachingem
- ✅ Persistent views pro restart-safe UI
- ✅ Rate limiting (1 ticket/5min per user)
- ✅ Kompletní error handling
- ✅ HTML/TXT transcript generator
- ✅ Admin příkazy (`!ticket setup`, `!ticket panel`, atd.)

### 3. **Integrace**
- ✅ Kompletní integrace do `main_integrated.py`
- ✅ Zachovány všechny audit logging funkce
- ✅ Zachován invite tracking a RP volby
- ✅ Přidán ticket management do help panelu

### 4. **Render Optimalizace**
- ✅ Fallback import systém pro deployment
- ✅ Optimalizovaný `keep_alive_optimized.py`
- ✅ Updated `requirements_integrated.txt`
- ✅ Emergency fallback třídy při import selhání

## 📁 Finální Struktura Souborů

```
/
├── main.py                      # Hlavní integrovaný bot
├── keep_alive.py                # Render-optimalizovaný Flask server
├── requirements.txt             # Všechny dependencies
├── .env                         # Environment variables (necommituj!)
├── DEPLOYMENT_GUIDE.md          # Tento návod
└── bot/                         # Modularizované komponenty
    ├── __init__.py
    ├── config/
    │   ├── __init__.py
    │   └── settings.py          # Centralizovaná konfigurace
    ├── database/
    │   ├── __init__.py
    │   └── manager.py           # DatabaseManager s retry logikou
    ├── tickets/
    │   ├── __init__.py
    │   ├── database.py          # Ticket database operations
    │   ├── manager.py           # TicketManager s rate limiting
    │   ├── views.py             # Persistent Discord UI views
    │   └── transcript.py        # HTML/TXT transcript generator
    ├── commands/
    │   ├── __init__.py
    │   └── tickets.py           # Kompletní ticket příkazy
    └── utils/
        ├── __init__.py
        ├── cache.py             # LRUCache implementace
        └── rate_limiter.py      # Rate limiting utility
```

**Celkem:** 17 Python souborů organizovaných do 6 modulů

## 🔧 Render Deployment Kroky

### 1. **Environment Variables v Render Dashboard**
```bash
TOKEN=your_discord_bot_token
DATABASE_URL=your_postgresql_url?sslmode=require
PYTHONPATH=.
```
⚠️ **DŮLEŽITÉ:** Nikdy necommituj `.env` do GitLabu! Používej Render Environment Variables.

### 2. **Build Settings**
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python main.py`

### 3. **Database URL Formát**
```
postgresql://user:password@host:port/database?sslmode=require
```

## 🎫 Ticket Systém - Použití

### Základní Nastavení
1. `!ticket setup` - Interaktivní nastavení
2. `!ticket mod_role @ModRole` - Nastavení moderátorské role
3. `!ticket transcript #channel` - Kanál pro transcripty
4. `!ticket add_button "Support" "Ahoj {user}, jak ti můžeme pomoci?"` - Přidání tlačítka

### Vytvoření Panelu
```
!ticket panel Vítejte v našem support systému!
```

### Kompletní Příkazy
- `!ticket_help` - Zobrazí všechny dostupné příkazy
- `!ticket settings` - Aktuální nastavení
- `!ticket ui menu/button` - Přepnutí mezi dropdown/tlačítka
- `!ticket close [důvod]` - Zavření ticketu

## 🔍 Testovací Checklist

### ✅ Database & Core
- [x] Bot se připojí k databázi
- [x] Cache systém funguje
- [x] Rate limiting aktivní
- [x] Audit logging funkční

### ✅ Ticket Systém
- [x] Persistent views po restartu
- [x] Ticket creation s permissions
- [x] Rate limiting (1 ticket/5min)
- [x] Transcript generování
- [x] Admin příkazy funkční

### ✅ Error Handling
- [x] Database failures graceful
- [x] Import failures s fallback
- [x] View interactions error-safe
- [x] Permission checks robustní

## 🚨 Možné Problémy & Řešení

### Import Errors
```python
# Automatický fallback systém v main_integrated.py
# Pokud selhává: zkontroluj PYTHONPATH=. v Render
```

### Database Connection Issues
```python
# SSL parametr v DATABASE_URL
# Retry logika s exponential backoff
# Graceful fallback na základní funkcionalitu
```

### Persistent Views
```python
# Views se automaticky obnovují po restartu
# Custom_id based persistence
# Error handling pro orphaned views
```

## 📊 Performance Features

- **Connection Pooling:** asyncpg pool (1-10 connections)
- **Caching:** LRU cache s expirací (guild: 30min, audit: 1min)
- **Rate Limiting:** Configurable per-feature limits
- **Memory Management:** Cleanup tasks každou hodinu
- **Debouncing:** Voice events s 5s debounce

## 🎯 Hlavní Funkce

### Moderní Bot Features
- ✅ Komprehensivní audit logging
- ✅ Invite tracking s cache
- ✅ Welcome/goodbye zprávy
- ✅ RP volební systém
- ✅ Voice state tracking s debouncing
- ✅ Message edit/delete logging
- ✅ Role/channel change tracking

### Nové Ticket Features
- ✅ Rate limited ticket creation
- ✅ Multi-type ticket support
- ✅ Transcript s HTML/TXT formáty
- ✅ Admin role management
- ✅ Persistent UI across restarts
- ✅ Comprehensive logging

## 🔗 Quick Start

1. Deploy na Render s `main.py`
2. Nastav environment variables
3. Bot automaticky vytvoří DB tabulky
4. `!ticket setup` pro ticket systém
5. `!help_panel` pro všechny příkazy

**Status:** 🟢 **Připraveno k deployment**