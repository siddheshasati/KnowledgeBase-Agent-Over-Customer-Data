# Deployment Fixes

## Issue 1: Local Port 8001 Already in Use

**Error:**
```
ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8001)
only one usage of each socket address (protocol/network address/port) is normally permitted
```

**Root cause:** A stale Python or uvicorn process is still holding port 8001.

**Fix:**
```powershell
# Find and kill the process
$proc = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess
if ($proc) { 
  Stop-Process -Id $proc -Force 
}

# Verify port is free
netstat -ano | findstr :8001

# Then restart
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

---

## Issue 2: Render Deployment Build Failures

**Error messages:**
```
Read-only file system (os error 30)
maturin failed
Cargo metadata failed
metadata-generation-failed
```

**Root cause:** Some Python packages are trying to compile from source code (native extensions) in the Render build environment, which:
- Has limited write access to certain directories
- May not have Rust/C compilers available
- Has read-only crate/cargo cache

**Solution:** Use prebuilt binary wheels instead of source compilation.

### Changes Made

#### 1. Updated `requirements.txt`
Added `gunicorn==23.0.0` for production WSGI server support and better error handling.

```txt
# Added to requirements.txt
gunicorn==23.0.0
```

#### 2. Updated `render.yaml`
Modified the build and start commands to:
- Use `--prefer-binary` flag to force binary wheel installation
- Use gunicorn with uvicorn workers for better production performance
- Explicitly specify Python 3.11

```yaml
buildCommand: "pip install --prefer-binary -r requirements.txt"
startCommand: "gunicorn -w 2 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT app.main:app"
pythonVersion: 3.11
```

### Why this works

- `--prefer-binary`: Tells pip to download prebuilt wheels (.whl) instead of compiling from source
- `gunicorn`: A proven production WSGI server that gracefully handles Render's execution model
- `UvicornWorker`: Adapter that lets gunicorn spawn and manage uvicorn worker processes

---

## Deployment Checklist for Render

### Before deploying:
1. ✅ Verify local app runs: `python -m uvicorn app.main:app --reload`
2. ✅ Commit `.env.production.example`, `render.yaml`, `Procfile`, `requirements.txt` to git
3. ✅ DO NOT commit `.env` with real API keys

### In Render dashboard:
1. Create new Web Service
2. Connect your Git repository
3. Set these **Environment Variables** (from `.env.production.example`):
   - `NEO4J_URI` → your Neo4j connection string
   - `NEO4J_USER` → neo4j username
   - `NEO4J_PASSWORD` → Neo4j password
   - `QDRANT_URL` → your Qdrant instance
   - `QDRANT_API_KEY` → Qdrant API key (if needed)
   - `POSTGRES_DSN` → PostgreSQL connection (optional, SQLite fallback will be used)
   - `GROQ_API_KEY` → from https://console.groq.com
   - `COHERE_API_KEY` → from https://dashboard.cohere.com

4. Click **Deploy**

### Expected build flow:
1. Render installs Python 3.11
2. Runs `pip install --prefer-binary -r requirements.txt`
3. All packages install from prebuilt wheels (no compilation)
4. Starts app with gunicorn + uvicorn

---

## If build still fails

If you still see read-only file system errors, try these additional steps:

### Option A: Simplify requirements (minimal deps)
```txt
fastapi==0.116.1
uvicorn[standard]==0.35.0
pydantic==2.11.7
pydantic-settings==2.10.1
python-dotenv==1.1.1
neo4j==5.28.2
httpx==0.28.1
beautifulsoup4==4.13.4
cohere==5.16.3
groq==0.31.0
tenacity==9.1.2
qdrant-client==1.15.1
gunicorn==23.0.0
```

### Option B: Use `runtime.txt` to lock Python version
Create `runtime.txt` in root:
```
python-3.11.9
```

### Option C: Skip psycopg binary and let SQLite fallback work
Remove or comment out psycopg line; the app already handles SQLite fallback gracefully.

---

## Local testing before Render deployment

Test the production build locally:
```powershell
# Install from scratch
pip install --prefer-binary -r requirements.txt

# Test with gunicorn
gunicorn -w 2 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000 app.main:app

# OR test with uvicorn (simpler for dev)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then test the `/health` endpoint:
```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8000/health | Select-Object -ExpandProperty Content
```

---

## Common Render gotchas

1. **Timeout during build:** Free tier has limited resources. If `pip install` takes >5 min, try Option A (simplify deps).
2. **Memory limit:** Render free tier is ~512MB. If app crashes, reduce worker count:
   ```yaml
   startCommand: "gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT app.main:app"
   ```
3. **Env vars not picked up:** Make sure to use Render's Environment Variables panel, not .env file (which won't be deployed).
4. **App crashes on startup:** Check Render logs in dashboard. The app will report health issues at `/health`.

---

## Next steps

1. Merge these fixes to your repo
2. Push to GitHub
3. Create Render service with updated `render.yaml`
4. Monitor the build logs
5. Once deployed, test `/health` endpoint
6. Test `/api/chat` with a sample question

If you hit any new errors, share the Render build log output and I can refine further.
