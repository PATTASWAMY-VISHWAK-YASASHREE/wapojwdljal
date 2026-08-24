# Deploy Guide — public server Twilio can reach (FREE)

## Why
Twilio's media servers won't connect to free tunnel domains
(ngrok-free.dev / trycloudflare.com). A real hosted domain fixes it.

## Option 1: Render.com (recommended, free tier) ♪

1. Go to https://render.com → sign up with Google (no card needed)
2. New + → Web Service → "Deploy from GitHub" 
   (or use "Deploy without Git" if easier: upload a zip)
3. Settings:
   - Runtime: Python 3
   - Build command:  pip install -r requirements.txt
   - Start command:  uvicorn app:app --host 0.0.0.0 --port $PORT
4. Environment variables → add:
   - TWILIO_ACCOUNT_SID = AC6e99...  (from .env)
   - TWILIO_AUTH_TOKEN  = a65300...
   - TWILIO_NUMBER      = +17372212163
   - PUBLIC_BASE_URL    = (leave blank for now)
   - OPENROUTER_API_KEY = sk-or-...
5. Create Web Service → you get: https://your-app.onrender.com

## After deployment

6. Test it: open https://your-app.onrender.com/health → {"status":"ok"}

7. Update .env locally:
   PUBLIC_BASE_URL=https://your-app.onrender.com

8. Make a test call:
   python tests/live_call.py 9885541788
   
9. Pick up → after trial notice, press any key on your phone keypad.
   Ananya speaks! Talk to her!

## Then the REAL call desu~ ★
python tests/live_call.py 8688664337

## Files in this folder
- app.py           → the server (verified working)
- requirements.txt → dependencies  
- Procfile         → start command
