# Telephone Translation System

Sinhala caller dials in → Reception Agent answers → Warm transfer to
fixed English speaker → Both joined in shared LiveKit room → Gemini
Translator handles real-time bi-directional Sinhala↔English translation.

No frontend. Backend only. Two Docker containers.

## Architecture

```
Sinhala Caller (dials in)
       ↓ inbound SIP trunk
Reception Agent (reception-agent/)
       ↓ warm transfer
English Speaker (TARGET_PHONE_NUMBER)
       ↓ both in shared room
Gemini Translator Agent (translator/)
       ↓ gemini-3.5-live-translate-preview
Sinhala caller hears English in Sinhala
English caller hears Sinhala in English
```

## Setup

1. Get a Gemini API key: aistudio.google.com → API Keys
2. Set up inbound SIP trunk in LiveKit Cloud dashboard
3. Create dispatch rule: inbound calls → agent_name="reception-agent"
4. Fill in .env.local (copy from .env.example)
5. Set TARGET_PHONE_NUMBER to the fixed English speaker's number

## Run

```bash
docker compose up --build
```

## What's still needed before first call

- GEMINI_API_KEY from aistudio.google.com
- TARGET_PHONE_NUMBER set to real E.164 number
- Inbound SIP trunk configured in LiveKit Cloud
- Dispatch rule: inbound → reception-agent
- Outbound trunk ST_JXw7kviFNjBw must be active
